#!/usr/bin/env python3
# Trigger-only commit: preregistered strategy rules unchanged.
from __future__ import annotations
import json, math, re
from dataclasses import dataclass
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

def sym_from_path(p):
    st=p.stem
    if st.endswith('_USDT'): return st.replace('_','')
    return st.replace('_','')

def load(root:Path):
    fs=sorted((root/'spot'/'4h').glob('*.parquet'))
    frames=[]
    for f in fs:
        s=sym_from_path(f)
        if not ok_symbol(s): continue
        try:x=pd.read_parquet(f,columns=['date','open','high','low','close','volume'])
        except Exception: continue
        x['date']=pd.to_datetime(x.date,utc=True).dt.tz_convert(None)
        x=x[(x.date>='2020-09-01')&(x.date<='2025-06-01')].copy()
        if len(x)<180: continue
        x['symbol']=s
        frames.append(x)
    if not frames: raise RuntimeError('no data')
    return pd.concat(frames,ignore_index=True).sort_values(['date','symbol']).drop_duplicates(['date','symbol'],keep='last')

def matrices(df):
    idx=pd.date_range(df.date.min(),df.date.max(),freq='4h')
    m={c:df.pivot(index='date',columns='symbol',values=c).reindex(idx) for c in ['open','high','low','close','volume']}
    c=m['close']; h=m['high']; l=m['low']; v=m['volume']; o=m['open']
    r7=c/c.shift(42)-1; r21=c/c.shift(126)-1; r30=c/c.shift(180)-1
    ret=c.pct_change(fill_method=None); rv7=ret.rolling(42,min_periods=42).std(); rv21=ret.rolling(126,min_periods=126).std()
    prev=c.shift(1); tr=pd.DataFrame(np.maximum.reduce([(h-l).values,(h-prev).abs().values,(l-prev).abs().values]),index=c.index,columns=c.columns)
    atr=tr.rolling(14,min_periods=14).mean()
    prev20h=h.shift(1).rolling(20,min_periods=20).max(); prev20v=v.shift(1).rolling(20,min_periods=20).median()
    liq=(v*c).rolling(180,min_periods=120).sum()/30.0
    history=c.notna().rolling(540,min_periods=1).sum()
    return m,r7,r21,r30,rv7,rv21,atr,prev20h,prev20v,liq,history

def maxdd(eq):
    s=pd.Series([x[1] for x in eq]); return float((s/s.cummax()-1).min()) if len(s) else 0

def run(root,out):
    df=load(root); m,r7,r21,r30,rv7,rv21,atr,p20h,p20v,liq,hist=matrices(df)
    idx=m['close'].index; syms=list(m['close'].columns)
    if 'BTCUSDT' not in syms: raise RuntimeError('BTC missing')
    trend=(r7>0)&(r21>0)&(liq>=5_000_000)&(hist>=540)
    disp=r7.where(trend).std(axis=1,skipna=True)
    disp_thr=disp.shift(1).rolling(180,min_periods=60).quantile(.90)
    start=pd.Timestamp('2021-01-01'); end=min(pd.Timestamp('2025-05-31 20:00'),idx.max())
    cash=100.0; positions={}; trades=[]; eq=[]; fees=0.; cooldown={}; exposure=[]
    pending_exits=set(); pending_entries=[]
    for i,t in enumerate(idx):
        if t<start or t>end: continue
        for s in list(pending_exits):
            p=positions.get(s)
            if not p: continue
            raw=m['open'].at[t,s]
            if pd.isna(raw): continue
            xp=float(raw)*0.9995; gross=p['qty']*xp; fee=gross*.001; cash+=gross-fee; fees+=fee
            pnl=(xp-p['entry'])*p['qty']-p['entry_fee']-fee
            trades.append({**p,'exit_time':t,'exit':xp,'exit_fee':fee,'pnl':pnl,'ret_on_notional':pnl/p['notional'],'reason':p.get('exit_reason','scheduled')})
            cooldown[s]=i+42; del positions[s]
        pending_exits.clear()
        if pending_entries:
            for ent in pending_entries:
                if len(positions)>=2: break
                s=ent['symbol']
                if s in positions: continue
                raw=m['open'].at[t,s]
                if pd.isna(raw): continue
                notional=min(ent['target_notional'],cash)
                if notional<1: continue
                ep=float(raw)*1.0005; fee=notional*.001; spend=notional+fee
                if spend>cash:
                    notional=cash/1.001; fee=notional*.001; spend=notional+fee
                qty=notional/ep; cash-=spend; fees+=fee
                a=float(ent['atr'])
                positions[s]={'symbol':s,'entry_time':t,'entry':ep,'qty':qty,'notional':notional,'entry_fee':fee,'stop':ep-2*a,'bars':0,'score':ent['score']}
            pending_entries=[]
        for s,p in list(positions.items()):
            lo=m['low'].at[t,s]
            if pd.notna(lo) and float(lo)<=p['stop']:
                xp=p['stop']*.9995; gross=p['qty']*xp; fee=gross*.001; cash+=gross-fee; fees+=fee
                pnl=(xp-p['entry'])*p['qty']-p['entry_fee']-fee
                trades.append({**p,'exit_time':t,'exit':xp,'exit_fee':fee,'pnl':pnl,'ret_on_notional':pnl/p['notional'],'reason':'stop'})
                cooldown[s]=i+42; del positions[s]
        equity=cash
        for s,p in positions.items():
            px=m['close'].at[t,s]
            if pd.notna(px): equity+=p['qty']*float(px)
        eq.append((t,equity)); exposure.append((t,len(positions)))
        btc7=r7.at[t,'BTCUSDT']; btc30=r30.at[t,'BTCUSDT']
        gate=pd.notna(btc7) and pd.notna(btc30) and btc7>0 and btc30>0
        for s,p in list(positions.items()):
            p['bars']+=1
            cl=m['close'].at[t,s]; a=atr.at[t,s]
            if pd.notna(cl) and pd.notna(a): p['stop']=max(p['stop'],float(cl)-2.5*float(a))
            if not gate:
                p['exit_reason']='btc_gate'; pending_exits.add(s)
            elif p['bars']>=42:
                p['exit_reason']='time'; pending_exits.add(s)
        if gate and len(positions)-len(pending_exits)<2 and pd.notna(disp.at[t]) and pd.notna(disp_thr.at[t]) and disp.at[t]<=disp_thr.at[t]:
            slots=2-(len(positions)-len(pending_exits)); cand=[]
            for s in syms:
                if s=='BTCUSDT' or s in positions or cooldown.get(s,-1)>i: continue
                vals=[r7.at[t,s],r21.at[t,s],rv7.at[t,s],rv21.at[t,s],atr.at[t,s],p20h.at[t,s],p20v.at[t,s],m['close'].at[t,s],m['volume'].at[t,s],liq.at[t,s],hist.at[t,s]]
                if any(pd.isna(x) for x in vals): continue
                a,b,x,y,at,hh,vmed,cl,volu,lq,hi=map(float,vals)
                if not(a>0 and b>0 and x>0 and y>0 and lq>=5_000_000 and hi>=540): continue
                if not(cl>hh and volu>=1.5*vmed): continue
                score=.60*(a/x)+.40*(b/y)
                cand.append((score,s,at))
            cand.sort(reverse=True)
            base_equity=equity
            for score,s,a in cand[:slots]: pending_entries.append({'symbol':s,'score':score,'atr':a,'target_notional':.5*base_equity})
    t=end if end in idx else idx[idx<=end][-1]
    for s,p in list(positions.items()):
        raw=m['close'].at[t,s]
        if pd.isna(raw): continue
        xp=float(raw)*.9995; gross=p['qty']*xp; fee=gross*.001; cash+=gross-fee; fees+=fee
        pnl=(xp-p['entry'])*p['qty']-p['entry_fee']-fee
        trades.append({**p,'exit_time':t,'exit':xp,'exit_fee':fee,'pnl':pnl,'ret_on_notional':pnl/p['notional'],'reason':'end'})
    e=pd.DataFrame(eq,columns=['date','equity']).drop_duplicates('date').set_index('date')
    tr=pd.DataFrame(trades)
    weekly=e.equity.resample('W-SUN').last().pct_change().dropna()
    ex=pd.DataFrame(exposure,columns=['date','npos']).set_index('date'); active=ex.npos.resample('W-SUN').max().reindex(weekly.index).fillna(0)>0
    wactive=weekly[active]
    oos=e[e.index>='2025-01-01']; oosret=float(oos.equity.iloc[-1]/oos.equity.iloc[0]-1) if len(oos)>1 else None
    oosw=oos.equity.resample('W-SUN').last().pct_change().dropna()
    years=(e.index[-1]-e.index[0]).total_seconds()/(365.25*86400)
    cagr=float((e.equity.iloc[-1]/100.)**(1/years)-1)
    rr=e.equity.pct_change().dropna(); sharpe=float(rr.mean()/rr.std()*math.sqrt(6*365.25)) if rr.std()>0 else None
    summary={
      'strategy':'Aggressive Spot v1 preregistered','data_symbols':int(df.symbol.nunique()),'start':str(e.index[0]),'end':str(e.index[-1]),
      'initial_nav':100.0,'final_nav':float(e.equity.iloc[-1]),'total_return':float(e.equity.iloc[-1]/100-1),'cagr':cagr,'max_drawdown':maxdd(eq),'sharpe_4h_ann':sharpe,
      'positions':int(len(tr)),'wins':int((tr.pnl>0).sum()) if len(tr) else 0,'win_rate':float((tr.pnl>0).mean()) if len(tr) else None,
      'avg_hold_days':float(((pd.to_datetime(tr.exit_time)-pd.to_datetime(tr.entry_time)).dt.total_seconds()/86400).mean()) if len(tr) else None,'fees_paid':fees,
      'weekly_mean':float(weekly.mean()),'weekly_median':float(weekly.median()),'weekly_positive_share':float((weekly>0).mean()),'weekly_negative_share':float((weekly<0).mean()),
      'weekly_ge_2pct_share':float((weekly>=.02).mean()),'weekly_ge_3pct_share':float((weekly>=.03).mean()),'weekly_best':float(weekly.max()),'weekly_worst':float(weekly.min()),
      'active_week_mean':float(wactive.mean()) if len(wactive) else None,'active_week_median':float(wactive.median()) if len(wactive) else None,'active_weeks':int(len(wactive)),'weeks':int(len(weekly)),
      'oos_2025_return':oosret,'oos_weekly_mean':float(oosw.mean()) if len(oosw) else None,'oos_weekly_median':float(oosw.median()) if len(oosw) else None,
      'hurdle_mean_weekly_2pct':bool(weekly.mean()>=.02),'risk_fail_mdd_gt_40pct':bool(maxdd(eq)<-.40)
    }
    out=Path(out); out.mkdir(parents=True,exist_ok=True)
    e.to_csv(out/'equity_4h.csv'); tr.to_csv(out/'trades.csv',index=False); pd.DataFrame({'weekly_return':weekly,'active':active}).to_csv(out/'weekly.csv')
    pd.DataFrame({'dispersion':disp,'threshold90':disp_thr}).loc[start:end].to_csv(out/'dispersion.csv')
    (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--out',default='aggressive_spot_v1_out');a=p.parse_args();run(Path(a.root),a.out)
