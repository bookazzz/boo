#!/usr/bin/env python3
import json, math
from pathlib import Path
import numpy as np
import pandas as pd
import regime_hybrid_v1_pit as rh

ROOT=Path('dataset'); OUT=Path('pit_extended'); OUT.mkdir(exist_ok=True)
cfg=rh.Config(); df=rh.load_daily(ROOT,cfg); mats,r30,r90,vol,liq,src=rh.matrices(df,cfg,None)
start,end=pd.Timestamp(cfg.start),pd.Timestamp(cfg.end); rebs=pd.date_range(start,end,freq='MS'); close=mats['close']

# Per-symbol normalized monthly contribution using exactly frozen execution, with NAV normalized to 100.
def contribution(reb, reg, side, sym):
    nxt=(reb+pd.offsets.MonthBegin(1)).normalize(); last=min(nxt-pd.Timedelta(days=1),end)
    cash=0.0; lots=[]; vetoed=False; bad=0; sched=False
    ent=[reb+pd.Timedelta(days=j) for j in range(cfg.entry_tranches) if reb+pd.Timedelta(days=j)<nxt and reb+pd.Timedelta(days=j)<=end]
    for d in pd.date_range(reb,last,freq='D'):
        if sched:
            for p in lots:
                if not p['active']: continue
                raw=mats['open'].at[d,sym]
                if pd.isna(raw): continue
                xp=raw*(1-cfg.slippage if side=='long' else 1+cfg.slippage)
                pnl=p['qty']*((xp-p['entry']) if side=='long' else (p['entry']-xp)); xf=abs(p['qty']*xp)*cfg.fee
                cash+=pnl-xf; p['active']=False
            vetoed=True; sched=False
        if d in ent and not vetoed:
            raw=mats['open'].at[d,sym] if sym in mats['open'].columns else np.nan
            if pd.notna(raw):
                e=raw*(1+cfg.slippage if side=='long' else 1-cfg.slippage); n=cfg.weight_per_slot*100/cfg.entry_tranches; q=n/e; cash-=n*cfg.fee
                lots.append({'entry':e,'qty':q,'active':True})
        for p in lots:
            if not p['active']: continue
            if sym not in mats['high'].columns or pd.isna(mats['close'].at[d,sym]):
                terminal=rh.find_terminal_exit(close,sym,d,min(nxt+pd.Timedelta(days=30),end))
                if terminal:
                    prev=close.loc[(close.index<d)&close[sym].notna(),sym]
                    if not prev.empty:
                        raw=float(prev.iloc[-1]); xp=raw*(1-cfg.slippage if side=='long' else 1+cfg.slippage)
                        pnl=p['qty']*((xp-p['entry']) if side=='long' else (p['entry']-xp)); xf=abs(p['qty']*xp)*cfg.fee
                        cash+=pnl-xf; p['active']=False
                        continue
                raise RuntimeError(f'data gap {sym} {d.date()}')
            hi=mats['high'].at[d,sym]; lo=mats['low'].at[d,sym]
            if side=='long' and lo<=p['entry']*(1-cfg.long_guard):
                xp=p['entry']*(1-cfg.long_guard)*(1-cfg.slippage); pnl=p['qty']*(xp-p['entry']); xf=p['qty']*xp*cfg.fee; cash+=pnl-xf; p['active']=False
            elif side=='short' and hi>=p['entry']*(1+cfg.short_guard):
                xp=p['entry']*(1+cfg.short_guard)*(1+cfg.slippage); pnl=p['qty']*(p['entry']-xp); xf=p['qty']*xp*cfg.fee; cash+=pnl-xf; p['active']=False
        rg=rh.regime_at(d,r30,r90); valid=(reg=='neutral') or (rg==reg); bad=0 if valid else bad+1
        if reg!='neutral' and bad>=cfg.veto_closes and not vetoed and not sched: sched=True
    for p in lots:
        if p['active']:
            raw=mats['open'].at[nxt,sym] if nxt<=end and sym in mats['open'].columns and pd.notna(mats['open'].at[nxt,sym]) else mats['close'].loc[:end,sym].dropna().iloc[-1]
            xp=raw*(1-cfg.slippage if side=='long' else 1+cfg.slippage); pnl=p['qty']*((xp-p['entry']) if side=='long' else (p['entry']-xp)); xf=p['qty']*xp*cfg.fee; cash+=pnl-xf; p['active']=False
    return cash/100.0

months=[]
cache={}
all_alts=[s for s in r30.columns if s!='BTCUSDT']
for reb in rebs:
    d=reb-pd.Timedelta(days=1); reg=rh.regime_at(d,r30,r90)
    def ok(s):
        vals=(r30.at[d,s],r90.at[d,s],vol.at[d,s],liq.at[d,s]); return all(pd.notna(v) for v in vals) and vals[3]>=cfg.liquidity_usd
    elig=[s for s in all_alts if ok(s)]
    if reg=='bull': cands=[s for s in elig if r30.at[d,s]>0 and r90.at[d,s]>0 and vol.at[d,s]>0]
    elif reg=='bear': cands=[s for s in elig if r30.at[d,s]<0 and r90.at[d,s]<0 and r30.at[d,s]>=-0.35]
    else: cands=[]
    # cache candidate contributions only where relevant
    if reg=='bull':
        if pd.notna(liq.at[d,'BTCUSDT']) and liq.at[d,'BTCUSDT']>=cfg.liquidity_usd: cache[(reb,'long','BTCUSDT')]=contribution(reb,reg,'long','BTCUSDT')
        for s in cands: cache[(reb,'long',s)]=contribution(reb,reg,'long',s)
    elif reg=='bear':
        for s in cands: cache[(reb,'short',s)]=contribution(reb,reg,'short',s)
    months.append({'reb':reb,'reg':reg,'d':d,'cands':cands})

# frozen deterministic selection from any permanent subset
def frozen_ret(m, subset=None):
    reb,reg,d,cands=m['reb'],m['reg'],m['d'],m['cands']
    allowed=cands if subset is None else [s for s in cands if s in subset]
    if reg=='bull':
        ret=cache.get((reb,'long','BTCUSDT'),0.0)
        if allowed:
            best=max(allowed,key=lambda s:r90.at[d,s]/vol.at[d,s]); ret+=cache[(reb,'long',best)]
        return ret
    if reg=='bear':
        picks=sorted(allowed,key=lambda s:r30.at[d,s],reverse=True)[:2]
        return sum(cache[(reb,'short',s)] for s in picks)
    return 0.0

baseline=np.array([frozen_ret(m) for m in months])
nav=100*np.cumprod(1+baseline); frozen_final=float(nav[-1]); oos_mask=np.array([m['reb']>=pd.Timestamp('2025-01-01') for m in months]); frozen_oos=float(np.prod(1+baseline[oos_mask])-1)
# Validate against Pass A summary.
a=json.load(open('pit_pass_a/summary.json'))
validation_error=abs(frozen_final-a['final_nav'])

rng=np.random.default_rng(20260901); N=10000
rnd_final=np.empty(N); rnd_oos=np.empty(N)
for k in range(N):
    rs=[]
    for m in months:
        reb,reg=m['reb'],m['reg']; c=m['cands']
        if reg=='bull':
            x=cache.get((reb,'long','BTCUSDT'),0.0)
            if c: x+=cache[(reb,'long',rng.choice(c))]
        elif reg=='bear':
            picks=rng.choice(c,size=min(2,len(c)),replace=False) if c else []
            x=sum(cache[(reb,'short',s)] for s in picks)
        else:x=0.0
        rs.append(x)
    rs=np.array(rs); rnd_final[k]=100*np.prod(1+rs); rnd_oos[k]=np.prod(1+rs[oos_mask])-1

# Permanent-universe perturbation: retain random 80% of all historical alts.
U=2000; subset_final=np.empty(U); subset_oos=np.empty(U)
for k in range(U):
    keep=set(rng.choice(all_alts,size=max(1,int(round(.8*len(all_alts)))),replace=False))
    rs=np.array([frozen_ret(m,keep) for m in months]); subset_final[k]=100*np.prod(1+rs); subset_oos[k]=np.prod(1+rs[oos_mask])-1

out={
 'frozen_reconstruction':{'final_nav':frozen_final,'oos_return':frozen_oos,'validation_abs_error_vs_pass_a':validation_error},
 'random_control':{
   'n':N,'full_percentile':float(np.mean(rnd_final<=frozen_final)*100),'oos_percentile':float(np.mean(rnd_oos<=frozen_oos)*100),
   'final_nav_p10':float(np.quantile(rnd_final,.1)),'final_nav_median':float(np.median(rnd_final)),'final_nav_p90':float(np.quantile(rnd_final,.9)),
   'oos_p10':float(np.quantile(rnd_oos,.1)),'oos_median':float(np.median(rnd_oos)),'oos_p90':float(np.quantile(rnd_oos,.9))},
 'universe_80pct_perturbation':{
   'n':U,'profitable_full_fraction':float(np.mean(subset_final>100)),'profitable_oos_fraction':float(np.mean(subset_oos>0)),
   'final_min':float(subset_final.min()),'final_p10':float(np.quantile(subset_final,.1)),'final_median':float(np.median(subset_final)),'final_p90':float(np.quantile(subset_final,.9)),
   'oos_min':float(subset_oos.min()),'oos_p10':float(np.quantile(subset_oos,.1)),'oos_median':float(np.median(subset_oos)),'oos_p90':float(np.quantile(subset_oos,.9))}
}
json.dump(out,open(OUT/'extended_acceptance.json','w'),indent=2); print(json.dumps(out,indent=2))
