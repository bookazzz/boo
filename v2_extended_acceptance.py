#!/usr/bin/env python3
import argparse,json
from pathlib import Path
import numpy as np
import pandas as pd
import regime_hybrid_v1_pit as rh
import regime_hybrid_v2_crossvenue as v2

def args():
    p=argparse.ArgumentParser(); p.add_argument('--signal-a',required=True); p.add_argument('--signal-b',required=True); p.add_argument('--exec-root',required=True); p.add_argument('--pass-a',required=True); p.add_argument('--out',required=True); p.add_argument('--n-random',type=int,default=10000); p.add_argument('--n-universe',type=int,default=2000); return p.parse_args()

def main():
    a=args(); cfg=rh.Config(); v2.prepare(Path(a.signal_a),Path(a.signal_b),Path(a.exec_root),cfg); C=v2.CTX; mats=C['e']['mats']; close=mats['close']; start,end=pd.Timestamp(cfg.start),pd.Timestamp(cfg.end); rebs=pd.date_range(start,end,freq='MS')
    def contribution(reb,reg,side,sym):
        nxt=(reb+pd.offsets.MonthBegin(1)).normalize(); last=min(nxt-pd.Timedelta(days=1),end); cash=0.; lots=[]; vetoed=False; bad=0; sched=False
        ent=[reb+pd.Timedelta(days=j) for j in range(cfg.entry_tranches) if reb+pd.Timedelta(days=j)<nxt and reb+pd.Timedelta(days=j)<=end]
        for d in pd.date_range(reb,last,freq='D'):
            if sched:
                for p in lots:
                    if not p['active']:continue
                    raw=mats['open'].at[d,sym]
                    if pd.isna(raw):continue
                    xp=raw*(1-cfg.slippage if side=='long' else 1+cfg.slippage); cash+=p['qty']*((xp-p['entry']) if side=='long' else (p['entry']-xp))-abs(p['qty']*xp)*cfg.fee; p['active']=False
                vetoed=True; sched=False
            if d in ent and not vetoed:
                raw=mats['open'].at[d,sym] if sym in mats['open'].columns else np.nan
                if pd.notna(raw):
                    e=raw*(1+cfg.slippage if side=='long' else 1-cfg.slippage); n=cfg.weight_per_slot*100/cfg.entry_tranches; cash-=n*cfg.fee; lots.append({'entry':e,'qty':n/e,'active':True})
            for p in lots:
                if not p['active']:continue
                if sym not in mats['high'].columns or pd.isna(mats['close'].at[d,sym]):
                    terminal=rh.find_terminal_exit(close,sym,d,min(nxt+pd.Timedelta(days=30),end))
                    if terminal:
                        prev=close.loc[(close.index<d)&close[sym].notna(),sym]
                        if not prev.empty:
                            raw=float(prev.iloc[-1]); xp=raw*(1-cfg.slippage if side=='long' else 1+cfg.slippage); cash+=p['qty']*((xp-p['entry']) if side=='long' else (p['entry']-xp))-abs(p['qty']*xp)*cfg.fee; p['active']=False; continue
                    raise RuntimeError(f'data gap {sym} {d.date()}')
                hi,lo=mats['high'].at[d,sym],mats['low'].at[d,sym]
                if side=='long' and lo<=p['entry']*(1-cfg.long_guard):
                    xp=p['entry']*(1-cfg.long_guard)*(1-cfg.slippage); cash+=p['qty']*(xp-p['entry'])-p['qty']*xp*cfg.fee; p['active']=False
                elif side=='short' and hi>=p['entry']*(1+cfg.short_guard):
                    xp=p['entry']*(1+cfg.short_guard)*(1+cfg.slippage); cash+=p['qty']*(p['entry']-xp)-p['qty']*xp*cfg.fee; p['active']=False
            rg=v2.cv_regime_at(d); bad=0 if (reg=='neutral' or rg==reg) else bad+1
            if reg!='neutral' and bad>=cfg.veto_closes and not vetoed and not sched:sched=True
        for p in lots:
            if p['active']:
                raw=mats['open'].at[nxt,sym] if nxt<=end and sym in mats['open'].columns and pd.notna(mats['open'].at[nxt,sym]) else mats['close'].loc[:end,sym].dropna().iloc[-1]; xp=raw*(1-cfg.slippage if side=='long' else 1+cfg.slippage); cash+=p['qty']*((xp-p['entry']) if side=='long' else (p['entry']-xp))-p['qty']*xp*cfg.fee; p['active']=False
        return cash/100.
    months=[]; cache={}
    for reb in rebs:
        st=v2.candidate_state(reb,cfg); reg=st['regime']; c=[s for s,_ in st['candidates']]
        if reg=='bull':
            if 'BTCUSDT' in C['common'] and v2.passes_liquidity(st['signal_date'],'BTCUSDT',cfg):cache[(reb,'long','BTCUSDT')]=contribution(reb,reg,'long','BTCUSDT')
            for s in c:cache[(reb,'long',s)]=contribution(reb,reg,'long',s)
        elif reg=='bear':
            for s in c:cache[(reb,'short',s)]=contribution(reb,reg,'short',s)
        months.append({'reb':reb,'reg':reg,'cands':c})
    def frozen_ret(m,subset=None):
        reb,reg=m['reb'],m['reg']; allowed=m['cands'] if subset is None else [s for s in m['cands'] if s in subset]
        if reg=='bull':
            x=cache.get((reb,'long','BTCUSDT'),0.); x+=cache[(reb,'long',allowed[0])] if allowed else 0.; return x
        if reg=='bear':return sum(cache[(reb,'short',s)] for s in allowed[:2])
        return 0.
    baseline=np.array([frozen_ret(m) for m in months]); frozen_final=float(100*np.prod(1+baseline)); oos=np.array([m['reb']>=pd.Timestamp('2025-01-01') for m in months]); frozen_oos=float(np.prod(1+baseline[oos])-1); ps=json.load(open(a.pass_a)); err=abs(frozen_final-ps['final_nav']); rng=np.random.default_rng(20260902)
    N=a.n_random; rf=np.empty(N); ro=np.empty(N)
    for k in range(N):
        rs=[]
        for m in months:
            reb,reg,c=m['reb'],m['reg'],m['cands']
            if reg=='bull':
                x=cache.get((reb,'long','BTCUSDT'),0.); x+=cache[(reb,'long',rng.choice(c))] if c else 0.
            elif reg=='bear':
                picks=rng.choice(c,size=min(2,len(c)),replace=False) if c else []; x=sum(cache[(reb,'short',s)] for s in picks)
            else:x=0.
            rs.append(x)
        rs=np.array(rs); rf[k]=100*np.prod(1+rs); ro[k]=np.prod(1+rs[oos])-1
    U=a.n_universe; uf=np.empty(U); uo=np.empty(U); alts=C['common_alts']
    for k in range(U):
        keep=set(rng.choice(alts,size=max(1,int(round(.8*len(alts)))),replace=False)); rs=np.array([frozen_ret(m,keep) for m in months]); uf[k]=100*np.prod(1+rs); uo[k]=np.prod(1+rs[oos])-1
    out={'frozen_reconstruction':{'final_nav':frozen_final,'oos_return':frozen_oos,'validation_abs_error_vs_pass_a':err},'random_control':{'n':N,'full_percentile':float(np.mean(rf<=frozen_final)*100),'oos_percentile':float(np.mean(ro<=frozen_oos)*100),'final_nav_p10':float(np.quantile(rf,.1)),'final_nav_median':float(np.median(rf)),'final_nav_p90':float(np.quantile(rf,.9)),'oos_p10':float(np.quantile(ro,.1)),'oos_median':float(np.median(ro)),'oos_p90':float(np.quantile(ro,.9))},'universe_80pct_perturbation':{'n':U,'profitable_full_fraction':float(np.mean(uf>100)),'profitable_oos_fraction':float(np.mean(uo>0)),'final_min':float(uf.min()),'final_p10':float(np.quantile(uf,.1)),'final_median':float(np.median(uf)),'final_p90':float(np.quantile(uf,.9)),'oos_min':float(uo.min()),'oos_p10':float(np.quantile(uo,.1)),'oos_median':float(np.median(uo)),'oos_p90':float(np.quantile(uo,.9))}}; Path(a.out).mkdir(parents=True,exist_ok=True); Path(a.out,'extended_acceptance.json').write_text(json.dumps(out,indent=2)); print(json.dumps(out,indent=2))
if __name__=='__main__':main()
