#!/usr/bin/env python3
from huggingface_hub import list_repo_files, hf_hub_download
import pandas as pd, json
files=list_repo_files('rogerdehe/klines-bybit',repo_type='dataset')
syms=['ADA','DOGE','WLFI','XPL']
out={'total_funding_files':sum('funding_rate' in f for f in files),'matches':{}}
for b in syms:
    matches=[f for f in files if 'funding_rate' in f and b in f]
    out['matches'][b]=matches[:50]
    if matches:
        f=matches[0]
        try:
            p=hf_hub_download('rogerdehe/klines-bybit',f,repo_type='dataset')
            x=pd.read_parquet(p)
            desc={}
            for c in x.columns:
                if c=='date':continue
                y=pd.to_numeric(x[c],errors='coerce')
                if y.notna().any():desc[c]={'min':float(y.min()),'max':float(y.max()),'mean':float(y.mean()),'nonzero':int((y.fillna(0)!=0).sum())}
            out['matches'][b+'_sample']={'file':f,'columns':list(x.columns),'head':x.head(5).astype(str).to_dict('records'),'describe':desc}
        except Exception as e:out['matches'][b+'_sample']={'file':f,'error':repr(e)}
print(json.dumps(out,indent=2))
open('funding_schema_audit.json','w').write(json.dumps(out,indent=2))
