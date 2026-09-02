#!/usr/bin/env python3
from __future__ import annotations
import json, math, re
from pathlib import Path
import numpy as np
import pandas as pd

EXCLUDE_BASE={'USDC','USDP','TUSD','FDUSD','BUSD','DAI','USDE','USDS','USD1','BFUSD','USUAL','USTC','EUR','TRY','BRL','GBP','AUD','BIDR','IDRT','RUB','UAH','NGN'}
SPECIAL_EXCLUDE={'BTCDOM','DEFI','FOOTBALL'}
TRADFI_RE=re.compile(r'^(AAPL|ADBE|AMD|AMZN|ASML|BABA|BRKB|COIN|COST|CRCL|CRM|CRWD|CSCO|DELL|DIS|DKNG|EBAY|GOOGL|HIMS|HOOD|HPE|IBM|INTC|IONQ|IREN|IWM|JPM|META|MSFT|MSTR|NFLX|NVDA|ORCL|PLTR|QQQ|SPY|TSLA|USO|EWJ|EWY|EWZ|HK0700|HK1810|HYUNDAI)$')

def ok_symbol(s):
    if not s.endswith('USDT'): return False
    b=s[:-4]
    if b in EXCLUDE_BASE or b in SPECIAL_EXCLUDE or TRADFI_RE.match(b): return False
    if any(x in b for x in ('UP','DOWN','BULL','BEAR')) and not b.startswith(('JUP','PUMP')): return False
    return True

def sym_from_path(p): return p.stem.replace('_','')

def load(root):
    fs=sorted((Path(root)/'spot'/'4h').glob('*.parquet')); frames=[]
    for f in fs:
        s=sym_from_path(f)
        if not ok_symbol(s): continue
        try:x=pd.read_parquet(f,columns=['date','open','high','low','close','volume'])
        except Exception: continue
        x['date']=pd.to_datetime(x.date,utc=True).dt.tz_convert(None)
        x=x[(x.date>='2020-06-01')&(x.date<='2025-06-01')].copy()
        if len(x)<180: continue
        x['symbol']=s; frames.append(x)
    if not frames: raise RuntimeError('no data')
    return pd.concat(frames,ignore_index=True).sort_values(['date','symbol']).drop_duplicates(['date','symbol'],keep='last')

def maxdd(s): return float((s/s.cummax()-1).min())

def run(root,out):
    df=load(root); idx=pd.date_range(df.date.min(),df.date.max(),freq='4h')
    mats={c:df.pivot(index='date',columns='symbol',values=c).reindex(idx) for c in ['open','close','volume']}
    c,o,v=mats['close'],mats['open'],mats['volume']; syms=list(c.columns)
    if 'BTCUSDT' not in syms: raise RuntimeError('BTC missing')
    r14=c/c.shift(84)-1; r30=c/c.shift(180)-1
    ret=c.pct_change(fill_method=None)
    btc_rv168=ret['BTCUSDT'].rolling(168,min_periods=168).std()
    btc_rv_med=btc_rv168.shift(1).rolling(1092,min_periods=546).median()
    liq=(v*c).rolling(180,min_periods=120).sum()/30.0
    hist=c.notna().rolling(540,min_periods=1).sum()
    start=pd.Timestamp('2021-01-01'); end=min(pd.Timestamp('2025-05-31 20:00'),idx.max())
    # weekly signal times Sunday 20:00 UTC
    sig_times=[t for t in idx if t>=start and t<=end and t.weekday()==6 and t.hour==20]
    weekly_disp=[]; plans={}
    for t in sig_times:
        eligible=[]
        for s in syms:
            vals=(liq.at[t,s],hist.at[t,s],r14.at[t,s])
            if all(pd.notna(z) for z in vals) and vals[0]>=5_000_000 and vals[1]>=540:
                eligible.append(s)
        top30=sorted(eligible,key=lambda s:float(liq.at[t,s]),reverse=True)[:30]
        rets=pd.Series({s:float(r14.at[t,s]) for s in top30 if pd.notna(r14.at[t,s])})
        disp=float(rets.std()) if len(rets)>=5 else np.nan
        prev=[d for tt,d in weekly_disp if tt<t]
        dthr=float(pd.Series(prev[-26:]).quantile(.90)) if len(prev)>=13 else np.nan
        weekly_disp.append((t,disp))
        btc14=r14.at[t,'BTCUSDT']; btc30=r30.at[t,'BTCUSDT']
        gate=bool(pd.notna(btc14) and pd.notna(btc30) and btc14>0 and btc30>0)
        dispersion_ok=bool(pd.notna(disp) and pd.notna(dthr) and disp<=dthr)
        rv=btc_rv168.at[t]; rvmed=btc_rv_med.at[t]
        exposure=.5 if (pd.notna(rv) and pd.notna(rvmed) and rv>rvmed) else 1.0
        selected=[]
        if gate and dispersion_ok and len(rets)>=5:
            q95=float(rets.quantile(.95))
            for s,val in rets.sort_values(ascending=False).items():
                if val<=0 or val>=q95: continue
                selected.append(s)
                if len(selected)>=2: break
        plans[t]={'top30':top30,'disp':disp,'dthr':dthr,'gate':gate,'dispersion_ok':dispersion_ok,'exposure':exposure,'selected':selected}
    cash=100.0; positions={}; fees=0.; eq=[]; legs=[]; current_week_selected=[]; current_week_active=False; risk_off=False
    sigset=set(sig_times)
    for t in idx:
        if t<start or t>end: continue
        # Monday open: apply yesterday's signal plan if available.
        if t.weekday()==0 and t.hour==0:
            prev=t-pd.Timedelta(hours=4); plan=plans.get(prev)
            # close all existing weekly positions conservatively, even retained names
            for s,p in list(positions.items()):
                raw=o.at[t,s]
                if pd.isna(raw): continue
                xp=float(raw)*.9995; gross=p['qty']*xp; fee=gross*.001; cash+=gross-fee; fees+=fee
                legs.append({'time':t,'symbol':s,'side':'sell','price':xp,'notional':gross,'fee':fee,'week_signal':prev,'reason':'weekly_rebalance'})
                del positions[s]
            current_week_selected=[]; current_week_active=False; risk_off=False
            if plan and plan['gate'] and plan['dispersion_ok'] and plan['selected']:
                target_each=plan['exposure']/len(plan['selected'])
                nav=cash
                for s in plan['selected']:
                    raw=o.at[t,s]
                    if pd.isna(raw): continue
                    notion=min(nav*target_each,cash/1.001)
                    if notion<1: continue
                    ep=float(raw)*1.0005; fee=notion*.001; qty=notion/ep; cash-=notion+fee; fees+=fee
                    positions[s]={'qty':qty,'entry':ep,'entry_time':t,'entry_notional':notion}
                    legs.append({'time':t,'symbol':s,'side':'buy','price':ep,'notional':notion,'fee':fee,'week_signal':prev,'reason':'weekly_entry'})
                    current_week_selected.append(s)
                current_week_active=bool(positions)
        # mark equity
        equity=cash
        for s,p in positions.items():
            px=c.at[t,s]
            if pd.notna(px): equity+=p['qty']*float(px)
        eq.append((t,equity,len(positions)))
        # intrawweek BTC risk exit after close, execute next bar open
        if positions:
            b14=r14.at[t,'BTCUSDT']; b30=r30.at[t,'BTCUSDT']
            gate=bool(pd.notna(b14) and pd.notna(b30) and b14>0 and b30>0)
            if not gate and not risk_off:
                risk_off=True
        if risk_off and positions:
            # schedule via immediate next iteration using a marker; emulate with direct next open if available
            j=idx.get_loc(t)
            if j+1<len(idx):
                nt=idx[j+1]
                if nt<=end:
                    for s,p in list(positions.items()):
                        raw=o.at[nt,s]
                        if pd.isna(raw): continue
                        xp=float(raw)*.9995; gross=p['qty']*xp; fee=gross*.001; cash+=gross-fee; fees+=fee
                        legs.append({'time':nt,'symbol':s,'side':'sell','price':xp,'notional':gross,'fee':fee,'week_signal':None,'reason':'btc_risk_exit'})
                        del positions[s]
                    risk_off=False
    # final liquidation
    t=end
    for s,p in list(positions.items()):
        raw=c.at[t,s]
        if pd.isna(raw): continue
        xp=float(raw)*.9995; gross=p['qty']*xp; fee=gross*.001; cash+=gross-fee;fees+=fee
        legs.append({'time':t,'symbol':s,'side':'sell','price':xp,'notional':gross,'fee':fee,'week_signal':None,'reason':'end'})
        del positions[s]
    e=pd.DataFrame(eq,columns=['date','equity','npos']).drop_duplicates('date',keep='last').set_index('date')
    # overwrite final equity to cash
    if len(e): e.loc[e.index[-1],'equity']=cash; e.loc[e.index[-1],'npos']=0
    weekly=e.equity.resample('W-SUN').last().pct_change().dropna()
    active=(e.npos.resample('W-SUN').max().reindex(weekly.index).fillna(0)>0)
    aw=weekly[active]
    oos=e[e.index>='2025-01-01']; oosret=float(oos.equity.iloc[-1]/oos.equity.iloc[0]-1) if len(oos)>1 else None; oosw=oos.equity.resample('W-SUN').last().pct_change().dropna()
    years=(e.index[-1]-e.index[0]).total_seconds()/(365.25*86400); cagr=float((e.equity.iloc[-1]/100)**(1/years)-1)
    rr=e.equity.pct_change().dropna(); sharpe=float(rr.mean()/rr.std()*math.sqrt(6*365.25)) if rr.std()>0 else None
    yr=e.equity.resample('YE').last().pct_change(); firstyr=float(e[e.index.year==e.index[0].year].equity.iloc[-1]/e.equity.iloc[0]-1); yearly={str(e.index[0].year):firstyr}; yearly.update({str(x.year):float(v) for x,v in yr.dropna().items()})
    summary={'strategy':'Aggressive Spot v2 preregistered','data_symbols':int(df.symbol.nunique()),'start':str(e.index[0]),'end':str(e.index[-1]),'initial_nav':100.0,'final_nav':float(e.equity.iloc[-1]),'total_return':float(e.equity.iloc[-1]/100-1),'cagr':cagr,'max_drawdown':maxdd(e.equity),'sharpe_4h_ann':sharpe,'execution_legs':len(legs),'buy_legs':sum(1 for x in legs if x['side']=='buy'),'fees_paid':fees,'weekly_mean':float(weekly.mean()),'weekly_median':float(weekly.median()),'weekly_positive_share':float((weekly>0).mean()),'weekly_negative_share':float((weekly<0).mean()),'weekly_ge_2pct_share':float((weekly>=.02).mean()),'weekly_ge_3pct_share':float((weekly>=.03).mean()),'weekly_best':float(weekly.max()),'weekly_worst':float(weekly.min()),'active_weeks':int(len(aw)),'active_week_mean':float(aw.mean()) if len(aw) else None,'active_week_median':float(aw.median()) if len(aw) else None,'oos_2025_return':oosret,'oos_weekly_mean':float(oosw.mean()) if len(oosw) else None,'oos_weekly_median':float(oosw.median()) if len(oosw) else None,'yearly_returns':yearly,'hurdle_mean_weekly_2pct':bool(weekly.mean()>=.02),'risk_fail_mdd_gt_40pct':bool(maxdd(e.equity)<-.40)}
    out=Path(out);out.mkdir(parents=True,exist_ok=True);e.to_csv(out/'equity_4h.csv');pd.DataFrame(legs).to_csv(out/'legs.csv',index=False);pd.DataFrame({'weekly_return':weekly,'active':active}).to_csv(out/'weekly.csv');pd.DataFrame([{'signal_time':t,**p} for t,p in plans.items()]).to_json(out/'weekly_plans.json',orient='records',indent=2);(out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8');print(json.dumps(summary,indent=2))

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--out',default='aggressive_spot_v2_out');a=p.parse_args();run(a.root,a.out)
