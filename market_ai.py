"""Read-only Toss market data + optional OpenAI paper-trading decisions.
No brokerage order endpoint is called anywhere in this module.
"""
import os, time, requests, json

BASE='https://openapi.tossinvest.com'
SYMBOL_NAMES={'AAPL':'애플','NVDA':'엔비디아','TSLA':'테슬라','AMD':'AMD','PLTR':'팔란티어'}
SYMBOLS=list(SYMBOL_NAMES)
_token=None
_expiry=0

def token():
 global _token,_expiry
 if _token and time.time() < _expiry-90:return _token
 cid=os.getenv('TOSS_CLIENT_ID'); secret=os.getenv('TOSS_CLIENT_SECRET')
 if not cid or not secret:raise RuntimeError('TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 미설정')
 r=requests.post(BASE+'/oauth2/token',data={'grant_type':'client_credentials','client_id':cid,'client_secret':secret},timeout=12)
 r.raise_for_status(); payload=r.json(); _token=payload['access_token'];_expiry=time.time()+int(payload.get('expires_in',3600));return _token

def quotes():
 r=requests.get(BASE+'/api/v1/prices',params={'symbols':','.join(SYMBOLS)},headers={'Authorization':'Bearer '+token()},timeout=12)
 r.raise_for_status(); data=r.json(); body=data.get('result',data) if isinstance(data,dict) else data
 if isinstance(body,dict):
  rows=next((body[k] for k in ('prices','items','stocks','data','quotes') if isinstance(body.get(k),list)),None)
  if rows is None: rows=[body] if 'symbol' in body else []
 elif isinstance(body,list):rows=body
 else:rows=[]
 result={}
 for item in rows:
  if not isinstance(item,dict):continue
  symbol=item.get('symbol') or item.get('code')
  value=next((item[k] for k in ('price','currentPrice','lastPrice','close','tradePrice') if item.get(k) is not None),None)
  if symbol in SYMBOLS and value is not None:
   try:
    price=float(value)
    if price>0:result[symbol]=price
   except (TypeError,ValueError):pass
 if not result:raise RuntimeError('시세 응답 필드 확인 필요: 공식 API 응답 형식과 파서 비교')
 return result

def gpt_decision(snapshot):
 key=os.getenv('OPENAI_API_KEY')
 if not key:return None
 prompt={'task':'US stock PAPER trading only. Decide one action BUY, SELL, or HOLD. No brokerage orders.', 'constraints':'Prefer HOLD unless clear signal. Return only JSON with action, symbol, reason. Never invent news or technical indicators.', 'snapshot':snapshot}
 body={'model':os.getenv('OPENAI_MODEL','gpt-4.1-mini'),'messages':[{'role':'system','content':'You are a conservative paper trading signal classifier. Output strict JSON only. No claims about unseen market news.'},{'role':'user','content':json.dumps(prompt,ensure_ascii=False)}],'response_format':{'type':'json_object'},'temperature':0}
 r=requests.post('https://api.openai.com/v1/chat/completions',headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},json=body,timeout=25)
 r.raise_for_status(); result=json.loads(r.json()['choices'][0]['message']['content'])
 if result.get('action') not in ('BUY','SELL','HOLD') or result.get('symbol') not in SYMBOLS:return None
 return result
