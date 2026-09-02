#!/usr/bin/env python3
from pathlib import Path
import json
import pandas as pd
import numpy as np
from huggingface_hub import hf_hub_download

T=Path('v2_bybit/trades.csv'); OUT=Path('v2_funding_replay'); OUT.mkdir(exist_ok=True)
t=pd.read_csv(T); t=t[t.side.eq('short')].copy(); t['entry_date']=pd.to_datetime(t.entry_date,utc=True); t['exit_date']=pd.to_datetime(t.exit_date,utc=True)
syms=sorted(t.sym.unique()); detail=[]; total=0.0; evn=0; covered=0; mark_cov_events=0; missing=[]
for s in syms:
    b=s[:-4]; ff=f'futures/1h/{b}_USDT_USDT-funding_rate.parquet'; mf=f'futures/1h/{b}_USDT_USDT-mark.parquet'
    try:
        fp=hf_hub_download('rogerdehe/klines-bybit',ff,repo_type='dataset'); f=pd.read_parquet(fp); f['date']=pd.to_datetime(f.date,utc=True); f['rate']=pd.to_numeric(f['open'],errors='coerce'); covered+=1
    except Exception as e:
        missing.append({'sym':s,'funding_error':repr(e)}); continue
    try:
        mp=hf_hub_download('rogerdehe/klines-bybit',mf,repo_type='dataset'); m=pd.read_parquet(mp); m['date']=pd.to_datetime(m.date,utc=True); m['mark']=pd.to_numeric(m['open'],errors='coerce'); mark_map=m.dropna(subset=['mark']).drop_duplicates('date').set_index('date')['mark']
    except Exception:
        mark_map=pd.Series(dtype=float)
    for idx,r in t[t.sym.eq(s)].iterrows():
        # entry modeled at daily open; funding exactly at entry timestamp is excluded because order/funding ordering is ambiguous.
        fx=f[(f.date>r.entry_date)&(f.date<r.exit_date)&f.rate.notna()].copy()
        trade_cf=0.0
        for _,fr in fx.iterrows():
            ts=fr.date; rate=float(fr.rate)
            if ts in mark_map.index and np.isfinite(mark_map.loc[ts]) and mark_map.loc[ts]>0:
                mark=float(mark_map.loc[ts]); mark_source='hourly_mark_open'; mark_cov_events+=1
            else:
                span=max((r.exit_date-r.entry_date).total_seconds(),1.0); frac=(ts-r.entry_date).total_seconds()/span; mark=float(r.entry)+(float(r.exit)-float(r.entry))*frac; mark_source='trade_price_interpolation'
            notional=float(r.qty)*mark
            # Short receives when rate positive, pays when rate negative.
            cf=notional*rate; trade_cf+=cf; total+=cf; evn+=1
            detail.append({'trade_index':int(idx),'sym':s,'funding_ts':str(ts),'rate':rate,'mark':mark,'mark_source':mark_source,'notional':notional,'short_funding_cashflow':cf})
pd.DataFrame(detail).to_csv(OUT/'funding_events.csv',index=False)
base=181.86217902408964
summary={'selected_short_symbols':len(syms),'funding_file_coverage_symbols':covered,'missing_symbols':missing,'funding_events':evn,'hourly_mark_covered_events':mark_cov_events,'hourly_mark_event_fraction':mark_cov_events/evn if evn else None,'net_short_funding_cashflow_usdt':total,'baseline_terminal_nav_ex_funding':base,'terminal_nav_with_funding_additive_approx':base+total,'funding_effect_pct_of_starting_100':total/100,'method':'actual Bybit dataset funding-rate open field at settlement timestamps; short cashflow = qty*mark*rate; hourly mark open where exact timestamp exists, otherwise trade-price interpolation; entry/exit-boundary settlements excluded'}
(OUT/'summary.json').write_text(json.dumps(summary,indent=2)); print(json.dumps(summary,indent=2))
