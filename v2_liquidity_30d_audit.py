#!/usr/bin/env python3
import gzip,io,json
from pathlib import Path
import pandas as pd, requests
OUT=Path('v2_liquidity_30d_audit'); OUT.mkdir(exist_ok=True)
syms=['WLFIUSDT','XPLUSDT']; days=pd.date_range('2026-06-01','2026-06-30',freq='D'); rows=[]
for s in syms:
    for d in days:
        url=f'https://public.bybit.com/trading/{s}/{s}{d:%Y-%m-%d}.csv.gz'; r=requests.get(url,timeout=90)
        rec={'sym':s,'date':str(d.date()),'status':r.status_code,'url':url}
        if r.status_code==200:
            try:
                x=pd.read_csv(io.BytesIO(gzip.decompress(r.content))); cols={c.lower():c for c in x.columns}; pc=cols.get('price'); sc=cols.get('size') or cols.get('qty') or cols.get('quantity')
                if pc and sc:
                    p=pd.to_numeric(x[pc],errors='coerce'); q=pd.to_numeric(x[sc],errors='coerce'); rec['turnover_usdt']=float((p*q).sum()); rec['rows']=len(x)
            except Exception as e:rec['error']=repr(e)
        rows.append(rec)
df=pd.DataFrame(rows); df.to_csv(OUT/'daily_turnover.csv',index=False)
summary={}
for s in syms:
    z=df[(df.sym==s)&df.turnover_usdt.notna()] if 'turnover_usdt' in df.columns else pd.DataFrame()
    summary[s]={'days_expected':30,'days_resolved':len(z),'avg_daily_turnover_usdt':float(z.turnover_usdt.mean()) if len(z) else None,'median_daily_turnover_usdt':float(z.turnover_usdt.median()) if len(z) else None,'min_daily_turnover_usdt':float(z.turnover_usdt.min()) if len(z) else None,'days_below_5m':int((z.turnover_usdt<5_000_000).sum()) if len(z) else None,'passes_30d_avg_5m':bool(len(z)==30 and z.turnover_usdt.mean()>=5_000_000)}
(OUT/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2))
