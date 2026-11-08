import os, sqlite3, time, threading, json, hmac
import market_ai
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo
import math
import market_hours
from flask import Flask, request, jsonify, render_template, session
from werkzeug.security import generate_password_hash, check_password_hash

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates")
)
app.secret_key=os.environ.get('SESSION_SECRET','development-only-change-me')
DB=os.environ.get('DB_PATH','data/trading.sqlite3')
SYMBOLS={s:0.0 for s in market_ai.SYMBOLS}
status={'source':'WAITING','error':'시세 연결 대기','last_quote':0,'last_gpt':0,'ai':'OPENAI_API_KEY 미설정' if not os.getenv('OPENAI_API_KEY') else 'GPT 대기'}
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
 # Real market quotes only. Never fall back to fabricated prices.
 try:
  latest=market_ai.quotes()
  with lock,db() as c:
   for symbol,price in latest.items():c.execute('UPDATE prices SET price=?,updated=? WHERE symbol=?',(price,time.time(),symbol))
  status.update(source='TOSS_REST',error='',last_quote=time.time())
 except Exception as exc:
  status.update(source='UNAVAILABLE',error=str(exc)[:180]);return
 with lock,db() as c:
  if get(c,'enabled')!='true':return
  if not market_hours.session_info()['tradable']:return
  if today_loss(c)>=float(get(c,'max_daily_loss')):
   setv(c,'enabled','false');log(c,'SYSTEM','HALT','일일 확정 손실 한도 도달');return
  fx=float(get(c,'fx'))
  positions=c.execute('SELECT * FROM positions').fetchall()
  for p in positions:
   row=c.execute('SELECT price,updated FROM prices WHERE symbol=?',(p['symbol'],)).fetchone()
   if not row or row['price']<=0 or time.time()-row['updated']>45:continue
   price=row['price'];peak=max(price,p['peak']);c.execute('UPDATE positions SET peak=? WHERE symbol=?',(peak,p['symbol']))
   pct=(price/p['entry']-1)*100;peakpct=(peak/p['entry']-1)*100
   reason=None
   if pct<=-float(get(c,'stop_loss')):reason='모의 손절'
   elif pct>=float(get(c,'take_profit')):reason='모의 익절'
   elif peakpct>=float(get(c,'early_exit')) and (peak-price)/peak>=.007:reason='고점 대비 0.7% 하락'
   if reason:paper_sell(c,p,price,fx,reason)
  if not os.getenv('OPENAI_API_KEY'):
   status['ai']='OPENAI_API_KEY 미설정 · 신규 매수 없음';return
  if time.time()-status['last_gpt']<int(os.getenv('GPT_INTERVAL_SECONDS','120')):return
  status['last_gpt']=time.time()
  snapshot={'prices':{r['symbol']:r['price'] for r in c.execute('SELECT * FROM prices WHERE price>0')},'positions':[dict(p) for p in c.execute('SELECT * FROM positions')],'cash_krw':float(get(c,'cash')),'fx_krw_per_usd':fx,'news':'미연결'}
 # Do not hold database lock during a remote AI request.
 try:
  decision=market_ai.gpt_decision(snapshot)
  if not decision:return
  action=decision['action'];symbol=decision['symbol'];reason=str(decision.get('reason','GPT 판단'))[:200]
  with lock,db() as c:
   if get(c,'enabled')!='true' or not market_hours.session_info()['tradable']:return
   row=c.execute('SELECT price,updated FROM prices WHERE symbol=?',(symbol,)).fetchone()
   if not row or row['price']<=0 or time.time()-row['updated']>45:return
   price=row['price'];fx=float(get(c,'fx'))
   log(c,symbol,action,reason,'gpt');status['ai']='GPT 판단 연결'
   if action=='BUY' and c.execute('SELECT count(*) n FROM positions').fetchone()['n']==0 and today_loss(c)<float(get(c,'max_daily_loss')):
    budget=min(float(get(c,'per_trade')),float(get(c,'cash')))
    if budget>=1000:
     shares=budget/(price*fx);setv(c,'cash',float(get(c,'cash'))-budget)
     c.execute('INSERT INTO positions VALUES (?,?,?,?)',(symbol,shares,price,price))
     c.execute('INSERT INTO trades(ts,symbol,side,shares,price,pnl,reason) VALUES(?,?,?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),symbol,'BUY',shares,price,0,reason))
   elif action=='SELL':
    pos=c.execute('SELECT * FROM positions WHERE symbol=?',(symbol,)).fetchone()
    if pos:paper_sell(c,pos,price,fx,reason)
 except Exception as exc:status['ai']='GPT 오류: '+str(exc)[:140]

def paper_sell(c,p,price,fx,reason):
 proceeds=p['shares']*price*fx;pnl=p['shares']*(price-p['entry'])*fx
 setv(c,'cash',float(get(c,'cash'))+proceeds)
 c.execute('DELETE FROM positions WHERE symbol=?',(p['symbol'],))
 c.execute('INSERT INTO trades(ts,symbol,side,shares,price,pnl,reason) VALUES(?,?,?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),p['symbol'],'SELL',p['shares'],price,pnl,reason))
 log(c,p['symbol'],'SELL',reason)

def worker():
 while True:
  try: tick()
  except Exception as e: print('worker error',repr(e),flush=True)
  time.sleep(max(10,int(os.getenv('PRICE_POLL_SECONDS','15'))))

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
def health():return jsonify(ok=True,mode='toss-rest-paper',source=status['source'],error=status['error'])

@app.get('/api/state')
def state():
 with lock,db() as c:
  cfg={k:get(c,k) for k in ('capital','cash','per_trade','max_daily_loss','enabled','take_profit','stop_loss','early_exit','fx')}
  prices={r['symbol']:round(r['price'],4) for r in c.execute('SELECT * FROM prices WHERE price>0')}
  positions=[dict(r) for r in c.execute('SELECT * FROM positions')]
  trades=[dict(r) for r in c.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 40')]
  decisions=[dict(r) for r in c.execute('SELECT * FROM decisions ORDER BY id DESC LIMIT 30')]
  fx=float(cfg['fx']);equity=float(cfg['cash'])+sum(p['shares']*prices.get(p['symbol'],p['entry'])*fx for p in positions)
  return jsonify(config=cfg,prices=prices,positions=positions,trades=trades,decisions=decisions,equity=round(equity,2),daily_loss=round(today_loss(c),2),data_source=status['source'],data_error=status['error'],last_quote=status['last_quote'],news='미연결',ai=status['ai'],market=market_hours.session_info(),symbols=market_ai.SYMBOL_NAMES,fx_note='고정 모의환율 (KRW/USD)')

@app.post('/api/settings')
def settings():
 payload=request.get_json(silent=True) or {}
 allowed={'per_trade':(1000,10000000),'max_daily_loss':(100,1000000),'take_profit':(1,30),'stop_loss':(1,20),'early_exit':(.5,10)}
 if 'enabled' in payload and not isinstance(payload['enabled'],bool):return jsonify(error='실행 설정이 올바르지 않습니다'),400
 values={}
 for k,(lo,hi) in {**allowed,'capital':(10000,10000000)}.items():
  if k in payload:
   try:v=float(payload[k])
   except (TypeError,ValueError):return jsonify(error=k+' 숫자를 확인해 주세요'),400
   if not math.isfinite(v) or not lo<=v<=hi:return jsonify(error=f'{k} 허용 범위: {lo:g} ~ {hi:g}'),400
   values[k]=v
 with lock,db() as c:
  current_capital=float(get(c,'capital'))
  capital=values.get('capital',current_capital)
  per_trade=values.get('per_trade',float(get(c,'per_trade')))
  if per_trade>capital:return jsonify(error='1회 투자금은 총 투자금보다 클 수 없습니다'),400
  if 'capital' in values and capital!=current_capital:
   if c.execute('SELECT count(*) n FROM trades').fetchone()['n']>0 or c.execute('SELECT count(*) n FROM positions').fetchone()['n']>0:
    return jsonify(error='거래 내역이 있어 초기 투자금을 변경할 수 없습니다. 기록 초기화가 필요합니다.'),409
  if payload.get('enabled') is True:
   if status['source']!='TOSS_REST' or time.time()-status['last_quote']>45:return jsonify(error='최신 실제 시세 연결 후 시작할 수 있습니다'),409
   if today_loss(c)>=values.get('max_daily_loss',float(get(c,'max_daily_loss'))):return jsonify(error='일일 손실 한도 도달'),409
  # Validate every field before writing anything; one save is one transaction.
  if capital!=current_capital:
   setv(c,'capital',capital);setv(c,'cash',capital)
  for k,v in values.items():
   if k!='capital':setv(c,k,v)
  if 'enabled' in payload:setv(c,'enabled',str(payload['enabled']).lower())
 return jsonify(ok=True,config=values)

if __name__=='__main__':
 init()
 threading.Thread(target=worker,daemon=True).start()
 app.run(host='0.0.0.0',port=int(os.environ.get('PORT','8080')),debug=False)
