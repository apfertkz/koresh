// WhatsApp-демон Кореша: сайдкар в том же контейнере. Держит ОДНУ сессию и шлёт текст по локальному HTTP.
// Только отправка: входящие не читаем и не храним (обработчика messages.upsert нет, историю не синхронизируем).
// Слушает 127.0.0.1, каждый запрос с заголовком X-WA-Token (случайный на запуск, его знает только Python-сервер).
// Перенесён из моста учредителя (Baileys 7.0.0-rc14), добавлены: токен, QR и код сопряжения по HTTP, выход, ветка loggedOut.
//
// Контракт:
//   GET  /status  -> {state: connecting|need_qr|connected|logged_out, linked: bool, qr: bool, contacts: N, connected_for: сек|null}
//   GET  /qr      -> {qr: "<строка QR>"|null}
//   GET  /contacts-> {contacts: [{name, number, saved}], count}   имена из синхронизации контактов WhatsApp (как видит WhatsApp Web)
//   POST /pair    {phone}      -> {ok:true, code:"ABCD-EFGH"} | {ok:false, error}
//   POST /send    {to, text}   -> {ok:true} | {ok:false, error}   (to: только цифры номера, с кодом страны)
//   POST /send    {self:true, text} -> то же, но на собственный номер привязанного аккаунта (служебное хозяину)
//   POST /logout  {}           -> {ok:true}   отвязать устройство, стереть сессию, поднять новый QR
//
// Переменные: WA_TOKEN (обязательна), WA_PORT (8790), WA_AUTH_DIR (./auth), WA_DATA_DIR (родитель WA_AUTH_DIR).

const http = require('http')
const crypto = require('crypto')
const path = require('path')
const fs = require('fs')

// Метки времени в логе: без них не сопоставить обрывы и отправки. Номера и тексты в лог не пишем никогда.
for (const m of ['log', 'error']) {
  const orig = console[m].bind(console)
  console[m] = (...a) => orig(new Date().toISOString(), ...a)
}

const TOKEN = process.env.WA_TOKEN || ''
if (!TOKEN) {
  console.error('[WA] WA_TOKEN не задан, демон не стартует')
  process.exit(2)
}
const AUTH_DIR = process.env.WA_AUTH_DIR || path.join(__dirname, 'auth')
const DATA_DIR = process.env.WA_DATA_DIR || path.dirname(AUTH_DIR)
const PORT = process.env.WA_PORT ? Number(process.env.WA_PORT) : 8790
const LINKED_FILE = path.join(DATA_DIR, 'linked.json')
const SENT_FILE = path.join(DATA_DIR, 'sent-store.json')
const CONTACTS_FILE = path.join(DATA_DIR, 'contacts-sync.json')
try { fs.mkdirSync(AUTH_DIR, { recursive: true, mode: 0o700 }) } catch {}
try { fs.chmodSync(AUTH_DIR, 0o700) } catch {}

const pino = require('pino')
const P = pino({ level: 'silent' })
const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} = require('@whiskeysockets/baileys')

let sock = null
let state = 'connecting'        // connecting | need_qr | connected | logged_out
let lastQR = null
let lostSession = false         // сессия была и слетела: остаёмся в logged_out, пока не привяжут заново
let starting = false
let reconnectTimer = null
let closesInRow = 0
let firstStart = true
let pairingInProgress = false

function setState(s) {
  if (s !== state) console.log('[WA] состояние:', state, '->', s)
  state = s
}
function isLinked() {
  try { return !!JSON.parse(fs.readFileSync(LINKED_FILE, 'utf8')).linked } catch { return false }
}
function markLinked() {
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 })
    const tmp = LINKED_FILE + '.tmp'
    fs.writeFileSync(tmp, JSON.stringify({ linked: true, at: Math.floor(Date.now() / 1000) }), { mode: 0o600 })
    fs.renameSync(tmp, LINKED_FILE)
  } catch (e) { console.error('[WA] не смог записать linked.json:', e && e.message || e) }
}
function unmarkLinked() { try { fs.unlinkSync(LINKED_FILE) } catch {} }
function forgetContacts() {
  if (contactsTimer) { clearTimeout(contactsTimer); contactsTimer = null }
  contactMap.clear()
  lidToPn.clear()
  connectedAt = 0
  try { fs.unlinkSync(CONTACTS_FILE) } catch {}
}

// Хранилище отправленных сообщений для getMessage: телефон получателя при сбое расшифровки шлёт retry-запрос,
// без исходного текста на телефоне вечное «Ожидание сообщения» (история моста: 397e9f8).
const SENT_TTL_MS = 24 * 3600 * 1000
const SENT_MAX = 500
const sentStore = new Map() // id -> { t, message }
try {
  const raw = JSON.parse(fs.readFileSync(SENT_FILE, 'utf8'))
  for (const [id, v] of Object.entries(raw)) sentStore.set(id, v)
} catch {}
function sentGc() {
  const now = Date.now()
  for (const [id, v] of sentStore) if (now - v.t > SENT_TTL_MS) sentStore.delete(id)
  while (sentStore.size > SENT_MAX) sentStore.delete(sentStore.keys().next().value)
}
function sentRemember(id, message) {
  if (!id || !message) return
  sentStore.set(id, { t: Date.now(), message })
  sentGc()
  try {
    const tmp = SENT_FILE + '.tmp'
    fs.writeFileSync(tmp, JSON.stringify(Object.fromEntries(sentStore)), { mode: 0o600 })
    fs.renameSync(tmp, SENT_FILE)
  } catch (e) { console.error('[WA] не смог сохранить sent-store:', e && e.message || e) }
}
async function getMessage(key) {
  sentGc()
  const v = key && sentStore.get(key.id)
  return v ? v.message : undefined
}
const msgRetryCounterCache = {
  m: new Map(),
  get(k) { const v = this.m.get(k); return v && v.e > Date.now() ? v.v : undefined },
  set(k, v) { this.m.set(k, { v, e: Date.now() + 3600 * 1000 }); return true },
  del(k) { this.m.delete(k) },
  flushAll() { this.m.clear() },
}

// ---------- контакты: только имена и номера из синхронизации, сообщений и чатов не трогаем ----------
const contactMap = new Map()   // ключ -> { id, lid, pn, name, notify }
let connectedAt = 0
let contactsTimer = null
const CONTACTS_MAX = 5000
const lidToPn = new Map()      // LID -> номер: в rc14 имена из записной книжки приходят под LID, номер подбираем отдельно

function pnOf(c) {
  const cand = [c.phoneNumber, c.id, c.jid]
  for (const x of cand) {
    const m = /^(\d{8,15})(?::\d+)?@s\.whatsapp\.net$/.exec(String(x || ''))
    if (m) return m[1]
  }
  return ''
}
function contactIngest(list) {
  if (!Array.isArray(list)) return
  for (const c of list) {
    if (!c || typeof c !== 'object' || !c.id) continue
    const id = String(c.id)
    if (id.endsWith('@g.us') || id.includes('broadcast') || id.endsWith('@newsletter')) continue
    const key = id
    const cur = contactMap.get(key) || { id }
    const pn = pnOf(c)
    if (pn) cur.pn = pn
    if (c.lid) cur.lid = String(c.lid)
    else if (id.endsWith('@lid')) cur.lid = id
    if (typeof c.name === 'string' && c.name.trim()) cur.name = c.name.trim()
    else if (typeof c.verifiedName === 'string' && c.verifiedName.trim() && !cur.name) cur.name = c.verifiedName.trim()
    if (typeof c.notify === 'string' && c.notify.trim()) cur.notify = c.notify.trim()
    contactMap.set(key, cur)
  }
  while (contactMap.size > CONTACTS_MAX) contactMap.delete(contactMap.keys().next().value)
  scheduleContactsSave()
}
function contactIngestLid(m) {
  if (!m || !m.lid) return
  const pn = pnOf({ id: m.pn })
  if (pn) { lidToPn.set(String(m.lid), pn); scheduleContactsSave() }
}
// Недостающие номера просим у репозитория соответствий LID<->номер самого Baileys (если он их знает)
async function resolveLids() {
  const s = sock
  const need = []
  for (const c of contactMap.values()) if (!c.pn && c.lid && !lidToPn.has(c.lid)) need.push(c.lid)
  if (!s || !need.length || !s.signalRepository || !s.signalRepository.lidMapping) return
  try {
    const res = await Promise.race([s.signalRepository.lidMapping.getPNsForLIDs(need.slice(0, 500)), new Promise((r) => setTimeout(() => r(null), 3000))])
    for (const m of res || []) contactIngestLid(m)
  } catch (e) { console.error('[WA] сопоставление LID не вышло:', e && e.name || typeof e) }
}
function contactList() {
  const byNum = new Map()
  for (const c of contactMap.values()) {
    const num = c.pn || lidToPn.get(c.lid || '') || ''
    if (!num) continue                                   // без номера писать некуда
    const name = c.name || c.notify || ''
    if (!name) continue
    const prev = byNum.get(num)
    const item = { name: name.slice(0, 60), number: num, saved: !!c.name }
    if (!prev || (item.saved && !prev.saved)) byNum.set(num, item)
  }
  return [...byNum.values()].sort((a, b) => a.name.localeCompare(b.name, 'ru'))
}
function scheduleContactsSave() {
  if (contactsTimer) return
  contactsTimer = setTimeout(async () => {
    contactsTimer = null
    await resolveLids()
    try {
      fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 })
      const tmp = CONTACTS_FILE + '.tmp'
      fs.writeFileSync(tmp, JSON.stringify({ at: Math.floor(Date.now() / 1000), contacts: contactList() }), { mode: 0o600 })
      fs.renameSync(tmp, CONTACTS_FILE)
    } catch (e) { console.error('[WA] не смог сохранить контакты:', e && e.message || e) }
  }, 1500)
}
try {
  const raw = JSON.parse(fs.readFileSync(CONTACTS_FILE, 'utf8'))
  for (const c of raw.contacts || []) contactMap.set(c.number + '@s.whatsapp.net', { id: c.number + '@s.whatsapp.net', pn: c.number, ...(c.saved ? { name: c.name } : { notify: c.name }) })
} catch {}

function wipeAuth() {
  try {
    for (const f of fs.readdirSync(AUTH_DIR)) fs.rmSync(path.join(AUTH_DIR, f), { recursive: true, force: true })
  } catch (e) { console.error('[WA] не смог стереть сессию:', e && e.message || e) }
}

function scheduleStart(ms) {
  if (reconnectTimer) return
  reconnectTimer = setTimeout(() => { reconnectTimer = null; start() }, ms)
}

// Закрыть предыдущий сокет перед созданием нового: две живые сессии на одних ключах дерутся
// (ключи шифрования расходятся, сообщения не расшифровываются).
function killOldSock() {
  const old = sock
  sock = null
  if (!old) return
  try { old.ev.removeAllListeners('connection.update') } catch {}
  try { old.ev.removeAllListeners('creds.update') } catch {}
  try { old.end(undefined) } catch {}
}

async function start() {
  if (starting) return
  starting = true
  try {
    killOldSock()
    let { state: auth, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
    if (firstStart) {
      firstStart = false
      // Брошенное сопряжение по коду оставляет в creds номер без подтверждения (me есть, account нет): такая сессия
      // не логинится и душит QR. Новый процесс начинает с чистого листа.
      if (auth.creds && auth.creds.me && !auth.creds.account) {
        console.log('[WA] недопривязанная сессия, стираю')
        wipeAuth()
        ;({ state: auth, saveCreds } = await useMultiFileAuthState(AUTH_DIR))
      }
    }
    const { version } = await fetchLatestBaileysVersion()
    lastQR = null
    if (state !== 'logged_out') setState('connecting')
    sock = makeWASocket({
      version,
      auth: { creds: auth.creds, keys: makeCacheableSignalKeyStore(auth.keys, P) },
      logger: P,
      printQRInTerminal: false,
      markOnlineOnConnect: false,
      syncFullHistory: false,           // полную историю не просим; стартовая синхронизация нужна ради имён контактов
      generateHighQualityLinkPreview: false,
      getMessage,
      msgRetryCounterCache,
    })
    const mine = sock
    mine.ev.on('creds.update', saveCreds)
    // Имена из синхронизации контактов и app-state (то, что видит WhatsApp Web). Сообщения, чаты и историю не читаем и не храним:
    // из messaging-history.set берём только массив contacts.
    mine.ev.on('contacts.upsert', (list) => { if (sock === mine) contactIngest(list) })
    mine.ev.on('contacts.update', (list) => { if (sock === mine) contactIngest(list) })
    mine.ev.on('messaging-history.set', (h) => { if (sock === mine && h) contactIngest(h.contacts) })
    mine.ev.on('lid-mapping.update', (m) => { if (sock === mine) contactIngestLid(m) })
    mine.ev.on('connection.update', (u) => {
      if (sock !== mine) return
      const { connection, lastDisconnect, qr } = u
      if (qr) {
        lastQR = qr
        if (state !== 'logged_out') setState('need_qr')
      }
      if (connection === 'open') {
        lastQR = null
        pairingInProgress = false
        lostSession = false
        closesInRow = 0
        setState('connected')
        connectedAt = Date.now()
        markLinked()
        console.log('[WA] подключён, готов отправлять')
      }
      if (connection === 'close') {
        const code = lastDisconnect && lastDisconnect.error && lastDisconnect.error.output && lastDisconnect.error.output.statusCode
        if (code === DisconnectReason.loggedOut) {
          console.log('[WA] разлогинен (код', code + '), стираю сессию, жду новую привязку')
          killOldSock()
          wipeAuth()
          forgetContacts()                 // другой аккаунт при перепривязке не должен получить чужие имена
          lastQR = null
          pairingInProgress = false
          lostSession = isLinked()
          setState(lostSession ? 'logged_out' : 'need_qr')
          closesInRow = 0
          scheduleStart(1000)
        } else {
          closesInRow += 1
          const wait = Math.min(60000, 3000 * Math.pow(2, Math.max(0, closesInRow - 1)))
          console.log('[WA] соединение закрыто, код', code, '- переподключаюсь через', Math.round(wait / 1000) + 'с')
          if (state === 'connected') setState('connecting')
          lastQR = state === 'need_qr' ? lastQR : null
          scheduleStart(code === DisconnectReason.restartRequired ? 500 : wait)
        }
      }
    })
  } catch (e) {
    console.error('[WA] старт не удался, повтор через 5с:', e && e.message || e)
    scheduleStart(5000)
  } finally {
    starting = false
  }
}

// Обрыв сети/таймаут Baileys бросает ошибку мимо connection.update и может убить процесс.
// Ловим на уровне процесса: не падаем, переподключаемся. Тексты ошибок могут содержать данные: пишем только тип.
process.on('unhandledRejection', (e) => {
  console.error('[WA] unhandledRejection:', e && e.name || typeof e)
  if (state === 'connected') setState('connecting')
  scheduleStart(3000)
})
process.on('uncaughtException', (e) => {
  console.error('[WA] uncaughtException:', e && e.name || typeof e)
  if (state === 'connected') setState('connecting')
  scheduleStart(3000)
})

function digitsOf(number) { return String(number || '').replace(/\D/g, '') }
function jidOf(number) { return digitsOf(number) + '@s.whatsapp.net' }

function tokenOk(req) {
  const got = Buffer.from(String(req.headers['x-wa-token'] || ''))
  const want = Buffer.from(TOKEN)
  return got.length === want.length && crypto.timingSafeEqual(got, want)
}

function readBody(req, cb) {
  let body = ''
  let tooBig = false
  req.on('data', (c) => { body += c; if (body.length > 20000) { tooBig = true; req.destroy() } })
  req.on('end', () => { if (!tooBig) cb(body) })
}

function prettyCode(c) {
  const s = String(c || '')
  return s.length === 8 ? s.slice(0, 4) + '-' + s.slice(4) : s
}

const server = http.createServer((req, res) => {
  const send = (code, obj) => {
    res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
    res.end(JSON.stringify(obj))
  }
  if (!tokenOk(req)) return send(401, { ok: false, error: 'unauthorized' })

  if (req.method === 'GET' && req.url === '/status') {
    return send(200, { ok: true, state, linked: isLinked(), qr: !!lastQR, contacts: contactList().length,
                       connected_for: state === 'connected' && connectedAt ? Math.floor((Date.now() - connectedAt) / 1000) : null })
  }
  if (req.method === 'GET' && req.url === '/contacts') {
    return resolveLids().then(() => {
      const list = contactList()
      send(200, { ok: true, contacts: list, count: list.length })
    })
  }
  if (req.method === 'GET' && req.url === '/qr') {
    return send(200, { ok: true, qr: lastQR })
  }
  if (req.method === 'POST' && req.url === '/pair') {
    return readBody(req, async (body) => {
      let phone
      try { ({ phone } = JSON.parse(body || '{}')) } catch { return send(200, { ok: false, error: 'bad_json' }) }
      const digits = digitsOf(phone)
      if (digits.length < 8 || digits.length > 15) return send(200, { ok: false, error: 'bad_phone' })
      if (state === 'connected') return send(200, { ok: false, error: 'already_connected' })
      if (!sock || !lastQR) return send(200, { ok: false, error: 'not_ready' })   // сокет ещё не дошёл до ожидания привязки
      try {
        const code = await sock.requestPairingCode(digits)
        pairingInProgress = true
        send(200, { ok: true, code: prettyCode(code) })
      } catch (e) {
        console.error('[WA] код сопряжения не получился:', e && e.name || typeof e)
        send(200, { ok: false, error: 'pair_failed' })
      }
    })
  }
  if (req.method === 'POST' && req.url === '/send') {
    return readBody(req, async (body) => {
      if (state !== 'connected' || !sock) return send(200, { ok: false, error: 'not_connected' })
      let to, text, self
      try { ({ to, text, self } = JSON.parse(body || '{}')) } catch { return send(200, { ok: false, error: 'bad_json' }) }
      if (!text || (!self && !digitsOf(to))) return send(200, { ok: false, error: 'missing_to_or_text' })
      if (String(text).length > 4000) return send(200, { ok: false, error: 'too_long' })
      try {
        let jid
        if (self) {
          // служебное сообщение хозяину «себе»: на собственный номер привязанного аккаунта (без суффикса устройства)
          const me = sock.user && sock.user.id
          if (!me) return send(200, { ok: false, error: 'no_self' })
          jid = String(me).replace(/:\d+(?=@)/, '')
        } else {
          const [result] = await sock.onWhatsApp(jidOf(to))
          if (!result || !result.exists) return send(200, { ok: false, error: 'not_on_whatsapp' })
          jid = result.jid
        }
        const sent = await sock.sendMessage(jid, { text: String(text) })
        if (sent && sent.key) sentRemember(sent.key.id, sent.message)
        console.log('[WA] отправлено')
        send(200, { ok: true })
      } catch (e) {
        console.error('[WA] отправка не вышла:', e && e.name || typeof e)
        send(200, { ok: false, error: 'send_failed' })
      }
    })
  }
  if (req.method === 'POST' && req.url === '/logout') {
    return readBody(req, async () => {
      try { if (sock && state === 'connected') await sock.logout() } catch (e) { console.error('[WA] logout:', e && e.name || typeof e) }
      killOldSock()
      wipeAuth()
      unmarkLinked()
      forgetContacts()
      lostSession = false
      lastQR = null
      pairingInProgress = false
      setState('need_qr')
      scheduleStart(500)
      send(200, { ok: true })
    })
  }
  send(404, { ok: false, error: 'not_found' })
})

server.listen(PORT, '127.0.0.1', () => console.log('[WA] демон слушает 127.0.0.1:' + PORT))
start().catch((e) => { console.error('[WA] старт не удался:', e && e.name || typeof e); process.exit(1) })
