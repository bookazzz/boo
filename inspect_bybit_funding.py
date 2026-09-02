#!/usr/bin/env python3
from huggingface_hub import hf_hub_download
import pandas as pd, json
syms=['ADA','DOGE','WLFI','XPL']
out={}
for b in syms:
    f=f'futures/8h/{b}_USDT_USDT-funding_rate.parquet'
    try:
        p=hf_hub_download('rogerdehe/klines-bybit',f,repo_type='dataset')
        x=pd.read_parquet(p)
        out[b]={'file':f,'columns':list(x.columns),'head':x.head(5).astype(str).to_dict('records'),'describe':{c:{'min':float(pd.to_numeric(x[c],errors='coerce').min()),'max':float(pd.to_numeric(x[c],errors='coerce').max()),'mean':float(pd.to_numeric(x[c],errors='coerce').mean()),'nonzero':int((pd.to_numeric(x[c],errors='coerce').fillna(0)!=0).sum())} for c in x.columns if c!='date'}}
    except Exception as e: out[b]={'file':f,'error':repr(e)}
print(json.dumps(out,indent=2))
open('funding_schema_audit.json','w').write(json.dumps(out,indent=2))
