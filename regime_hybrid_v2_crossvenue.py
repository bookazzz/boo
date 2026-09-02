#!/usr/bin/env python3
"""Regime Hybrid Cap2 v2 — Cross-Venue Consensus.
Frozen before holdout: Binance+Bybit consensus signals; all v1 risk/execution rules unchanged.
"""
from __future__ import annotations
import argparse,json
from pathlib import Path
import pandas as pd
import regime_hybrid_v1_pit as rh
CTX={}

def load_panel(root,cfg):
    df=rh.load_daily(root,cfg); mats,r30,r90,vol,liq,src=rh.matrices(df,cfg,None)
    return {'root':root,'df':df,'mats':mats,'r30':r30,'r90':r90,'vol':vol,'liq':liq,'src':src}

def prepare(signal_a,signal_b,exec_root,cfg):
    global CTX
    a=load_panel(signal_a,cfg); b=load_panel(signal_b,cfg)
    er,ar,br=exec_root.resolve(),signal_a.resolve(),signal_b.resolve(); e=a if er==ar else (b if er==br else load_panel(exec_root,cfg))
    common=set(a['r30'].columns)&set(b['r30'].columns)&set(e['r30'].columns)
    CTX={'a':a,'b':b,'e':e,'cfg':cfg,'common':sorted(common),'common_alts':sorted(s for s in common if s!='BTCUSDT')}; return CTX

def cv_regime_at(d,*_):
    a,b=CTX['a'],CTX['b']
    if 'BTCUSDT' not in CTX['common'] or d not in a['r30'].index or d not in b['r30'].index:return 'neutral'
    rs=[]
    for p in (a,b):
        x,y=p['r30'].at[d,'BTCUSDT'],p['r90'].at[d,'BTCUSDT']
        if pd.isna(x) or pd.isna(y):return 'neutral'
        rs.append('bull' if x>0 and y>0 else ('bear' if x<0 and y<0 else 'neutral'))
    return rs[0] if rs[0]==rs[1] and rs[0] in ('bull','bear') else 'neutral'

def passes_liquidity(d,s,cfg):
    for p in (CTX['a'],CTX['b'],CTX['e']):
        if s not in p['liq'].columns:return False
        v=p['liq'].at[d,s]
        if pd.isna(v) or v<cfg.liquidity_usd:return False
    return True

def candidate_state(reb,cfg=None):
    cfg=cfg or CTX['cfg']; d=reb-pd.Timedelta(days=1); reg=cv_regime_at(d); a,b=CTX['a'],CTX['b']; c=[]
    if reg=='bull':
        for s in CTX['common_alts']:
            if not passes_liquidity(d,s,cfg):continue
            vals=[a['r30'].at[d,s],a['r90'].at[d,s],a['vol'].at[d,s],b['r30'].at[d,s],b['r90'].at[d,s],b['vol'].at[d,s]]
            if any(pd.isna(v) for v in vals) or vals[2]<=0 or vals[5]<=0:continue
            if vals[0]>0 and vals[1]>0 and vals[3]>0 and vals[4]>0:c.append((s,float(min(vals[1]/vals[2],vals[4]/vals[5]))))
        c.sort(key=lambda z:z[1],reverse=True)
    elif reg=='bear':
        for s in CTX['common_alts']:
            if not passes_liquidity(d,s,cfg):continue
            vals=[a['r30'].at[d,s],a['r90'].at[d,s],b['r30'].at[d,s],b['r90'].at[d,s]]
            if any(pd.isna(v) for v in vals):continue
            if vals[0]<0 and vals[1]<0 and vals[2]<0 and vals[3]<0 and vals[0]>=-.35 and vals[2]>=-.35:c.append((s,float((vals[0]+vals[2])/2)))
        c.sort(key=lambda z:z[1],reverse=True)
    return {'reb':reb,'signal_date':d,'regime':reg,'candidates':c}

def cv_select(reb,cfg,*_):
    st=candidate_state(reb,cfg); reg,c,d=st['regime'],st['candidates'],st['signal_date']; out=[]
    if reg=='bull':
        if 'BTCUSDT' in CTX['common'] and passes_liquidity(d,'BTCUSDT',cfg):out=[('long','BTCUSDT')]
        if c:out.append(('long',c[0][0]))
    elif reg=='bear':out=[('short',s) for s,_ in c[:cfg.cap]]
    return reg,out[:cfg.cap]

def parse_args():
    p=argparse.ArgumentParser(); p.add_argument('--signal-a',required=True); p.add_argument('--signal-b',required=True); p.add_argument('--exec-root',required=True); p.add_argument('--out',required=True); p.add_argument('--start',default='2022-01-01'); p.add_argument('--end',default='2026-07-08'); p.add_argument('--disable-pass-b',action='store_true'); return p.parse_args()

def run(args):
    cfg=rh.Config(start=args.start,end=args.end); prepare(Path(args.signal_a),Path(args.signal_b),Path(args.exec_root),cfg); old_s,old_r=rh.select,rh.regime_at; rh.select,rh.regime_at=cv_select,cv_regime_at
    try:
        e=CTX['e']; res=rh.backtest(e['root'],cfg,e['mats'],e['r30'],e['r90'],e['vol'],e['liq'],use_pass_b=not args.disable_pass_b)
    finally:rh.select,rh.regime_at=old_s,old_r
    out=Path(args.out); out.mkdir(parents=True,exist_ok=True); res['equity'].to_csv(out/'equity_daily.csv'); res['monthly'].to_csv(out/'monthly.csv',index=False); res['trades'].to_csv(out/'trades.csv',index=False); res['incidents'].to_csv(out/'incidents.csv',index=False)
    s={k:v for k,v in res.items() if k not in {'equity','monthly','trades','incidents'}}; s.update({'strategy':'Regime Hybrid Cap2 v2 Cross-Venue Consensus','signal_a_symbols':int(CTX['a']['df'].symbol.nunique()),'signal_b_symbols':int(CTX['b']['df'].symbol.nunique()),'exec_symbols':int(CTX['e']['df'].symbol.nunique()),'common_symbols':len(CTX['common']),'common_alts':len(CTX['common_alts']),'liquidity_source':'volume_times_close_estimate_all_venues','pass_b_enabled':not args.disable_pass_b}); (out/'summary.json').write_text(json.dumps(s,indent=2,ensure_ascii=False)); print(json.dumps(s,indent=2,ensure_ascii=False)); return s
if __name__=='__main__':run(parse_args())
