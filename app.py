import os, sqlite3, time, random, threading, json, hmac
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template, session
from werkzeug.security import generate_password_hash, check_password_hash

app=Flask(__name__)
app.secret_key=os.environ.get('SESSION_SECRET','development-only-change-me')
DB=os.environ.get('DB_PATH','data/trading.sqlite3')
SYMBOLS={'AAPL':190.0,'NVDA':130.0,'TSLA':250.0,'AMD':150.0,'PLTR':70.0}
lock=threading.RLock()

def db():
 c=sqlite3.connect(DB,timeout=15); c.row_factory=sqlite3.Row; return c

def init():
 os.makedirs(os.path.dirname(DB) or '.',exist_ok=True)
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
  c.execute('CREATE TABLE IF NOT EXISTS prices (symbol TEXT PRIMARY KEY, price REAL NOT NULL, updated REAL NOT NULL)')
  c.execute('CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, shares REAL NOT NULL, entry REAL NOT NULL, peak REAL NOT NULL)')
  c.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, shares REAL, price REAL, pnl REAL, reason TEXT)')
  c.execute('CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, action TEXT, reason TEXT, engine TEXT)')
  defaults={'capital':'100000','cash':'100000','per_trade':'30000','max_daily_loss':'3000','enabled':'false','take_profit':'5','stop_loss':'3','early_exit':'1','fx':'1400','last_news_poll':'0'}
  for k,v in defaults.items(): c.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',(k,v))
  for k,v in SYMBOLS.items(): c.execute('INSERT OR IGNORE INTO prices VALUES (?,?,?)',(k,v,time.time()))

def get(c,k): return c.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone()['value']
def setv(c,k,v): c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(k,str(v)))
def log(c,s,a,reason,engine='rules'):
 c.execute('INSERT INTO decisions(ts,symbol,action,reason,engine) VALUES(?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),s,a,reason,engine))
def today_loss(c):
 today=datetime.now(timezone.utc).date().isoformat()
 return -sum(min(0,r['pnl']) for r in c.execute("SELECT pnl FROM trades WHERE side='SELL' AND substr(ts,1,10)=?",(today,)))
def tick():
 with lock,db() as c:
  prices=c.execute('SELECT * FROM prices').fetchall()
  for p in prices:
   # Synthetic random-walk data, NOT live market data
   new=max(.01,p['price']*random.uniform(.995,1.005))
   c.execute('UPDATE prices SET price=?,updated=? WHERE symbol=?',(new,time.time(),p['symbol']))
  if get(c,'enabled')!='true': return
  if today_loss(c)>=float(get(c,'max_daily_loss')):
   setv(c,'enabled','false');log(c,'SYSTEM','HALT','일일 손실 한도 도달');return
  fx=float(get(c,'fx')); cash=float(get(c,'cash'))
  pos=c.execute('SELECT * FROM positions').fetchall()
  for p in pos:
   price=c.execute('SELECT price FROM prices WHERE symbol=?',(p['symbol'],)).fetchone()['price']
   peak=max(price,p['peak']); c.execute('UPDATE positions SET peak=? WHERE symbol=?',(peak,p['symbol']))
   pct=(price/p['entry']-1)*100
   peakpct=(peak/p['entry']-1)*100
   reason=None
   if pct<=-float(get(c,'stop_loss')): reason='손절 기준 도달'
   elif pct>=float(get(c,'take_profit')): reason='목표 익절 도달'
   elif peakpct>=float(get(c,'early_exit')) and (peak-price)/peak>=.007: reason='고점 대비 0.7% 하락으로 조기 익절/청산'
   if reason:
    proceeds=p['shares']*price*fx; pnl=p['shares']*(price-p['entry'])*fx
    setv(c,'cash',float(get(c,'cash'))+proceeds)
    c.execute('DELETE FROM positions WHERE symbol=?',(p['symbol'],))
    c.execute('INSERT INTO trades(ts,symbol,side,shares,price,pnl,reason) VALUES(?,?,?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),p['symbol'],'SELL',p['shares'],price,pnl,reason))
    log(c,p['symbol'],'SELL',reason)
  if c.execute('SELECT count(*) n FROM positions').fetchone()['n']>0:return
  # conservative example rule: occasional entry, no fabricated GPT claim
  if random.random()>.035:return
  s=random.choice(list(SYMBOLS)); price=c.execute('SELECT price FROM prices WHERE symbol=?',(s,)).fetchone()['price']
  budget=min(float(get(c,'per_trade')),float(get(c,'cash')))
  if budget<1000:return
  shares=budget/(price*fx)
  setv(c,'cash',float(get(c,'cash'))-budget)
  c.execute('INSERT INTO positions VALUES (?,?,?,?)',(s,shares,price,price))
  c.execute('INSERT INTO trades(ts,symbol,side,shares,price,pnl,reason) VALUES(?,?,?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),s,'BUY',shares,price,0,'가상 시세 규칙 기반 진입'))
  log(c,s,'BUY','가상 시세 규칙 기반 진입')

def worker():
 while True:
  try: tick()
  except Exception as e: print('worker error',repr(e),flush=True)
  time.sleep(5)

@app.before_request
def guard():
 if request.path in ('/login','/health') or request.path.startswith('/static/'):return
 if not session.get('authenticated'):
  if request.path.startswith('/api/'):return jsonify(error='login required'),401
  return render_template('login.html')

@app.post('/login')
def login():
 password=os.environ.get('DASHBOARD_PASSWORD','')
 if len(password)<12:return jsonify(error='Set DASHBOARD_PASSWORD (12+ characters) in .env'),503
 if hmac.compare_digest(request.form.get('password',''),password):
  session['authenticated']=True;return ('',302,{'Location':'/'})
 return render_template('login.html',error='비밀번호가 올바르지 않습니다'),401

@app.get('/')
def home():return render_template('index.html')

@app.get('/health')
def health():return jsonify(ok=True,mode='synthetic-paper')

@app.get('/api/state')
def state():
 with lock,db() as c:
  cfg={k:get(c,k) for k in ('capital','cash','per_trade','max_daily_loss','enabled','take_profit','stop_loss','early_exit','fx')}
  prices={r['symbol']:round(r['price'],4) for r in c.execute('SELECT * FROM prices')}
  positions=[dict(r) for r in c.execute('SELECT * FROM positions')]
  trades=[dict(r) for r in c.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 40')]
  decisions=[dict(r) for r in c.execute('SELECT * FROM decisions ORDER BY id DESC LIMIT 30')]
  fx=float(cfg['fx']);equity=float(cfg['cash'])+sum(p['shares']*prices[p['symbol']]*fx for p in positions)
  return jsonify(config=cfg,prices=prices,positions=positions,trades=trades,decisions=decisions,equity=round(equity,2),daily_loss=round(today_loss(c),2),data_source='SYNTHETIC',news='미연결',ai='미연결: 규칙 기반')

@app.post('/api/settings')
def settings():
 payload=request.get_json(silent=True) or {}
 allowed={'per_trade':(1000,10000000),'max_daily_loss':(100,1000000),'take_profit':(1,30),'stop_loss':(1,20),'early_exit':(.5,10)}
 with lock,db() as c:
  if 'enabled' in payload:
   if not isinstance(payload['enabled'],bool):return jsonify(error='invalid enabled'),400
   if payload['enabled'] and today_loss(c)>=float(get(c,'max_daily_loss')):return jsonify(error='daily loss limit'),409
   setv(c,'enabled',str(payload['enabled']).lower())
  for k,(lo,hi) in allowed.items():
   if k in payload:
    try: v=float(payload[k])
    except (TypeError,ValueError):return jsonify(error='invalid '+k),400
    if not lo<=v<=hi:return jsonify(error='out of range '+k),400
    if k=='per_trade' and v>float(get(c,'capital')):return jsonify(error='per trade exceeds capital'),400
    setv(c,k,v)
  if 'capital' in payload:
   try:v=float(payload['capital'])
   except (TypeError,ValueError):return jsonify(error='invalid capital'),400
   if not 10000<=v<=10000000:return jsonify(error='capital range 10000..10000000'),400
   if c.execute('SELECT count(*) n FROM trades').fetchone()['n']>0 and v!=float(get(c,'capital')):return jsonify(error='capital change only before first trade; reset required'),409
   setv(c,'capital',v);setv(c,'cash',v)
 return jsonify(ok=True)

if __name__=='__main__':
 init()
 threading.Thread(target=worker,daemon=True).start()
 app.run(host='0.0.0.0',port=int(os.environ.get('PORT','8080')),debug=False)
