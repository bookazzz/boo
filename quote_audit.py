#!/usr/bin/env python3
import gzip,json,shutil
from pathlib import Path
import requests,duckdb,pandas as pd

OUT=Path('exact_quote_volume.csv')
meta=requests.get('https://api.github.com/repos/terrylica/binance-futures-availability/releases/latest',timeout=60).json()
asset=next(a for a in meta['assets'] if a['name']=='availability.duckdb.gz')
r=requests.get(asset['browser_download_url'],stream=True,timeout=180); r.raise_for_status()
gz=Path('availability.duckdb.gz')
with open(gz,'wb') as f:
    for chunk in r.iter_content(1024*1024):
        if chunk:f.write(chunk)
with gzip.open(gz,'rb') as src,open('availability.duckdb','wb') as dst: shutil.copyfileobj(src,dst)
con=duckdb.connect('availability.duckdb',read_only=True)
df=con.execute("""
SELECT date,symbol,quote_volume_usdt
FROM daily_availability
WHERE available=true
  AND quote_volume_usdt IS NOT NULL
  AND date BETWEEN DATE '2021-09-01' AND DATE '2026-07-10'
ORDER BY date,symbol
""").df(); con.close(); df.to_csv(OUT,index=False)
info={'rows':len(df),'symbols':int(df.symbol.nunique()),'min_date':str(df.date.min()),'max_date':str(df.date.max()),'release':meta.get('tag_name'),'asset':asset['name']}
Path('exact_quote_volume.meta.json').write_text(json.dumps(info,indent=2)); print(json.dumps(info,indent=2))
