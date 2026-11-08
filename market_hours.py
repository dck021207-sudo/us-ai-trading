"""US equity market session display. NY timezone handles US daylight saving.
Holiday calendar is advisory; live quotes and provider rules remain authoritative.
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

NY=ZoneInfo('America/New_York')
KR=ZoneInfo('Asia/Seoul')

def session_info(now=None):
 now=(now or datetime.now(NY)).astimezone(NY)
 t=now.time()
 holiday=False
 try:
  import holidays
  holiday=now.date() in holidays.financial_holidays('NYSE',years=now.year)
 except Exception:
  pass
 if now.weekday()>=5 or holiday:
  code,label='CLOSED','휴장 (주말/미국 증시 휴일)'
 elif time(4)<=t<time(9,30):
  code,label='PRE','프리마켓'
 elif time(9,30)<=t<time(16):
  code,label='REGULAR','정규장'
 elif time(16)<=t<time(20):
  code,label='AFTER','애프터마켓'
 else:
  code,label='CLOSED','장 마감'
 return {'code':code,'label':label,'tradable':code in ('PRE','REGULAR','AFTER'),
         'ny_time':now.strftime('%Y-%m-%d %H:%M %Z'),
         'kr_time':now.astimezone(KR).strftime('%Y-%m-%d %H:%M KST'),
         'schedule':'미국 동부시간 프리 04:00–09:30 / 정규 09:30–16:00 / 애프터 16:00–20:00',
         'note':'조기 폐장·종목별 거래 제한·실제 시세 제공 시간은 별도 확인 필요'}
