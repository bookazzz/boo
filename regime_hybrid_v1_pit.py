#!/usr/bin/env python3
"""Regime Hybrid v1 — survivorship-free PIT backtest runner.

Expected input layout (rogerdehe/klines-binance):
  <root>/futures/1d/*_USDT_USDT.parquet
Optional second-pass data:
  <root>/futures/1h/*_USDT_USDT-mark.parquet
  <root>/futures/8h/*_USDT_USDT-funding_rate.parquet

Frozen v1 rules:
- UTC D1 signals only, prior closed day.
- BTC regime: BULL if R30>0 & R90>0; BEAR if both <0; else NEUTRAL.
- BULL: BTC + best alt by R90/Vol90 among R30>0,R90>0.
- BEAR: up to 2 least-oversold downtrend alts: R30<0,R90<0,R30>=-35%, rank R30 desc.
- Cap2; 20% NAV per slot (max gross 40%).
- Monthly rebalance, 3-day staggered entry.
- Long Guard12; Short Guard25.
- 3 consecutive daily BTC regime invalidations => exit entire directional sleeve next open; cash until next rebalance.
- Fee 10bp/side + slippage 5bp/side baseline.
- One shared NAV.
"""
from __future__ import annotations
import argparse, json, re
from dataclasses import dataclass, asdict
from pathlib import Path
import numpy as np
import pandas as pd

EXCLUDE_BASE={'USDC','USDP','TUSD','FDUSD','BUSD','DAI','USDE','USDS','USD1','BFUSD','USUAL','USTC'}
TRADFI_RE=re.compile(r'^(AAPL|ADBE|AMD|AMZN|ASML|BABA|BRKB|COIN|COST|CRCL|CRM|CRWD|CSCO|DELL|DIS|DKNG|EBAY|GOOGL|HIMS|HOOD|HPE|IBM|INTC|IONQ|IREN|IWM|JPM|META|MSFT|MSTR|NFLX|NVDA|ORCL|PLTR|QQQ|SPY|TSLA|USO|EWJ|EWY|EWZ|HK0700|HK1810|HYUNDAI)$')
SPECIAL_EXCLUDE={'BTCDOM','DEFI','FOOTBALL'}

@dataclass(frozen=True)
class Config:
    start:str='2022-01-01'; end:str='2026-07-08'; initial_nav:float=100.0
    fast:int=30; slow:int=90; vol_window:int=90; weight_per_slot:float=0.20; cap:int=2
    liquidity_usd:float=5_000_000.0; liquidity_window:int=30; min_history:int=90
    long_guard:float=0.12; short_guard:float=0.25; fee:float=0.001; slippage:float=0.0005
    entry_tranches:int=3; veto_closes:int=3

def parse_args():
    p=argparse.ArgumentParser(); p.add_argument('--root',required=True); p.add_argument('--out',default='out')
    p.add_argument('--start',default='2022-01-01'); p.add_argument('--end',default='2026-07-08')
    p.add_argument('--exact-liquidity-csv',default=None); p.add_argument('--disable-pass-b',action='store_true'); return p.parse_args()

def canonical_symbol(path:Path)->str:
    stem=path.stem
    if stem.endswith('-mark') or stem.endswith('-funding_rate'): stem=stem.rsplit('-',1)[0]
    if stem.endswith('_USDT_USDT'): return f'{stem[:-10]}USDT'
    return stem.replace('_','')

def crypto_symbol_ok(sym:str)->bool:
    if not sym.endswith('USDT'): return False
    base=sym[:-4]
    if base in EXCLUDE_BASE or base in SPECIAL_EXCLUDE or TRADFI_RE.match(base): return False
    if any(x in base for x in ('UP','DOWN','BULL','BEAR')) and not base.startswith(('JUP','PUMP')): return False
    return True

def load_daily(root:Path,cfg:Config)->pd.DataFrame:
    files=sorted((root/'futures'/'1d').glob('*.parquet'))
    if not files: raise FileNotFoundError(f'No parquet files under {root}/futures/1d')
    frames=[]; pre=pd.Timestamp(cfg.start)-pd.Timedelta(days=max(cfg.slow,cfg.vol_window,cfg.liquidity_window)+5); end=pd.Timestamp(cfg.end)+pd.Timedelta(days=2)
    for f in files:
        if '-mark' in f.stem or '-funding_rate' in f.stem: continue
        sym=canonical_symbol(f)
        if not crypto_symbol_ok(sym): continue
        try: x=pd.read_parquet(f,columns=['date','open','high','low','close','volume'])
        except Exception as e: print('WARN',f.name,e); continue
        x['date']=pd.to_datetime(x['date'],utc=True).dt.tz_convert(None).dt.normalize(); x=x[(x.date>=pre)&(x.date<=end)].copy()
        if x.empty: continue
        x['symbol']=sym; x['dollar_volume_est']=pd.to_numeric(x.volume,errors='coerce')*pd.to_numeric(x.close,errors='coerce')
        frames.append(x[['date','symbol','open','high','low','close','volume','dollar_volume_est']])
    if not frames: raise RuntimeError('No usable daily crypto futures rows')
    return pd.concat(frames,ignore_index=True).sort_values(['date','symbol']).drop_duplicates(['date','symbol'],keep='last')

def matrices(df,cfg,exact_liq=None):
    idx=pd.date_range(pd.Timestamp(cfg.start)-pd.Timedelta(days=100),pd.Timestamp(cfg.end)+pd.Timedelta(days=2),freq='D')
    mats={c:df.pivot(index='date',columns='symbol',values=c).reindex(idx) for c in ['open','high','low','close','dollar_volume_est']}
    close=mats['close']; r30=close/close.shift(cfg.fast)-1; r90=close/close.shift(cfg.slow)-1
    vol=close.pct_change(fill_method=None).rolling(cfg.vol_window,min_periods=cfg.vol_window).std()
    if exact_liq:
        q=pd.read_csv(exact_liq); q['date']=pd.to_datetime(q.date).dt.normalize(); qmat=q.pivot(index='date',columns='symbol',values='quote_volume_usdt').reindex(idx)
        liq=qmat.rolling(cfg.liquidity_window,min_periods=20).mean(); src='exact_quote_volume'
    else:
        liq=mats['dollar_volume_est'].rolling(cfg.liquidity_window,min_periods=20).mean(); src='volume_times_close_estimate'
    return mats,r30,r90,vol,liq,src

def regime_at(d,r30,r90):
    if d not in r30.index:return 'neutral'
    a=r30.at[d,'BTCUSDT']; b=r90.at[d,'BTCUSDT']
    if pd.isna(a) or pd.isna(b): return 'neutral'
    return 'bull' if a>0 and b>0 else ('bear' if a<0 and b<0 else 'neutral')

def select(reb,cfg,r30,r90,vol,liq):
    d=reb-pd.Timedelta(days=1); reg=regime_at(d,r30,r90); syms=[s for s in r30.columns if s!='BTCUSDT']
    def ok(s):
        vals=(r30.at[d,s],r90.at[d,s],vol.at[d,s],liq.at[d,s]); return all(pd.notna(v) for v in vals) and vals[3]>=cfg.liquidity_usd
    elig=[s for s in syms if ok(s)]
    if reg=='bull':
        out=[]
        if pd.notna(liq.at[d,'BTCUSDT']) and liq.at[d,'BTCUSDT']>=cfg.liquidity_usd: out=[('long','BTCUSDT')]
        c=[s for s in elig if r30.at[d,s]>0 and r90.at[d,s]>0 and vol.at[d,s]>0]
        if c: out.append(('long',max(c,key=lambda s:r90.at[d,s]/vol.at[d,s])))
        return reg,out[:cfg.cap]
    if reg=='bear':
        c=[s for s in elig if r30.at[d,s]<0 and r90.at[d,s]<0 and r30.at[d,s]>=-0.35]
        return reg,[('short',s) for s in sorted(c,key=lambda s:r30.at[d,s],reverse=True)[:cfg.cap]]
    return reg,[]

def find_terminal_exit(close,sym,d,horizon):
    fut=close.loc[(close.index>d)&(close.index<=horizon),sym] if sym in close.columns else pd.Series(dtype=float); return not fut.notna().any()

def load_mark_daily(root,sym,start,end):
    f=root/'futures'/'1h'/f'{sym[:-4]}_USDT_USDT-mark.parquet'
    if not f.exists(): return None
    x=pd.read_parquet(f,columns=['date','open','high','low','close']); x['date']=pd.to_datetime(x.date,utc=True).dt.tz_convert(None)
    x=x[(x.date>=start)&(x.date<end+pd.Timedelta(days=1))].set_index('date')
    return None if x.empty else x.resample('1D').agg({'open':'first','high':'max','low':'min','close':'last'})

def load_funding(root,sym,start,end):
    f=root/'futures'/'8h'/f'{sym[:-4]}_USDT_USDT-funding_rate.parquet'
    if not f.exists(): return None
    x=pd.read_parquet(f); 
    if 'date' not in x.columns:return None
    x['date']=pd.to_datetime(x.date,utc=True).dt.tz_convert(None); x=x[(x.date>=start)&(x.date<=end+pd.Timedelta(days=1))].copy()
    rc=[c for c in x.columns if c.lower() in ('funding_rate','fundingrate','close','rate')]
    if not rc:return None
    x['rate']=pd.to_numeric(x[rc[0]],errors='coerce'); return x[['date','rate']].dropna()

def backtest(root,cfg,mats,r30,r90,vol,liq,use_pass_b=True):
    start,end=pd.Timestamp(cfg.start),pd.Timestamp(cfg.end); rebs=pd.date_range(start,end,freq='MS')
    nav=cfg.initial_nav; eq=[]; trades=[]; months=[]; incidents=[]; selected_short_syms=set(); funding_total=0.0; close=mats['close']
    for i,reb in enumerate(rebs):
        nxt=rebs[i+1] if i+1<len(rebs) else end+pd.Timedelta(days=1); reg,sel=select(reb,cfg,r30,r90,vol,liq)
        st=nav; cash=nav; lots=[]; vetoed=False; bad=0; sched=False; ent=[reb+pd.Timedelta(days=j) for j in range(cfg.entry_tranches) if reb+pd.Timedelta(days=j)<nxt and reb+pd.Timedelta(days=j)<=end]
        last=min(nxt-pd.Timedelta(days=1),end); mark_cache={}; funding_cache={}
        for side,s in sel:
            if side=='short':
                selected_short_syms.add(s)
                if use_pass_b: mark_cache[s]=load_mark_daily(root,s,reb,last); funding_cache[s]=load_funding(root,s,reb,last)
        for d in pd.date_range(reb,last,freq='D'):
            if sched:
                for p in lots:
                    if not p['active']:continue
                    raw=mats['open'].at[d,p['sym']]
                    if pd.isna(raw):continue
                    xp=raw*(1-cfg.slippage if p['side']=='long' else 1+cfg.slippage); pnl=p['qty']*((xp-p['entry']) if p['side']=='long' else (p['entry']-xp)); xf=abs(p['qty']*xp)*cfg.fee
                    cash+=pnl-xf; p.update(active=False,exit_date=d,exit=xp,reason='regime_veto',exit_fee=xf)
                vetoed=True; sched=False
            if d in ent and not vetoed:
                for side,s in sel:
                    raw=mats['open'].at[d,s] if s in mats['open'].columns else np.nan
                    if pd.isna(raw): incidents.append({'date':str(d.date()),'symbol':s,'type':'ENTRY_MISSING'}); continue
                    e=raw*(1+cfg.slippage if side=='long' else 1-cfg.slippage); n=cfg.weight_per_slot*st/cfg.entry_tranches; q=n/e; ef=n*cfg.fee; cash-=ef
                    lots.append({'side':side,'sym':s,'entry':e,'qty':q,'active':True,'entry_date':d,'entry_fee':ef,'exit_fee':0.,'exit_date':None,'exit':None,'reason':None,'funding':0.})
            for p in lots:
                if not p['active']:continue
                s=p['sym']; row=None
                if p['side']=='short' and use_pass_b and mark_cache.get(s) is not None and d in mark_cache[s].index: row=mark_cache[s].loc[d]
                else:
                    if s not in mats['high'].columns or pd.isna(mats['close'].at[d,s]):
                        terminal=find_terminal_exit(close,s,d,min(nxt+pd.Timedelta(days=30),end))
                        if terminal:
                            prev=close.loc[(close.index<d)&close[s].notna(),s]
                            if not prev.empty:
                                raw=float(prev.iloc[-1]); xp=raw*(1-cfg.slippage if p['side']=='long' else 1+cfg.slippage); pnl=p['qty']*((xp-p['entry']) if p['side']=='long' else (p['entry']-xp)); xf=abs(p['qty']*xp)*cfg.fee
                                cash+=pnl-xf; p.update(active=False,exit_date=d,exit=xp,reason='delist_exit',exit_fee=xf); incidents.append({'date':str(d.date()),'symbol':s,'type':'DELIST_EXIT'}); continue
                        incidents.append({'date':str(d.date()),'symbol':s,'type':'SELECTED_DATA_GAP'}); raise RuntimeError(f'Selected data gap {s} {d.date()}')
                    row=pd.Series({'high':mats['high'].at[d,s],'low':mats['low'].at[d,s],'close':mats['close'].at[d,s]})
                if p['side']=='long' and row['low']<=p['entry']*(1-cfg.long_guard):
                    xp=p['entry']*(1-cfg.long_guard)*(1-cfg.slippage); pnl=p['qty']*(xp-p['entry']); xf=p['qty']*xp*cfg.fee; cash+=pnl-xf; p.update(active=False,exit_date=d,exit=xp,reason='guard12',exit_fee=xf)
                elif p['side']=='short' and row['high']>=p['entry']*(1+cfg.short_guard):
                    xp=p['entry']*(1+cfg.short_guard)*(1+cfg.slippage); pnl=p['qty']*(p['entry']-xp); xf=p['qty']*xp*cfg.fee; cash+=pnl-xf; p.update(active=False,exit_date=d,exit=xp,reason='guard25',exit_fee=xf)
            if use_pass_b:
                for p in lots:
                    if not p['active'] or p['side']!='short':continue
                    fx=funding_cache.get(p['sym'])
                    if fx is None:continue
                    rows=fx[fx.date.dt.normalize()==d]
                    if rows.empty:continue
                    mark=mats['close'].at[d,p['sym']]
                    for rate in rows.rate:
                        cf=p['qty']*mark*float(rate); cash+=cf; p['funding']+=cf; funding_total+=cf
            rg=regime_at(d,r30,r90); valid=(reg=='neutral') or (rg==reg); bad=0 if valid else bad+1
            if reg!='neutral' and bad>=cfg.veto_closes and not vetoed and not sched:sched=True
            evalue=cash
            for p in lots:
                if p['active']:
                    px=mats['close'].at[d,p['sym']]; evalue+=p['qty']*((px-p['entry']) if p['side']=='long' else (p['entry']-px))
            eq.append((d,evalue))
        for p in lots:
            if p['active']:
                raw=mats['open'].at[nxt,p['sym']] if nxt<=end and p['sym'] in mats['open'].columns and pd.notna(mats['open'].at[nxt,p['sym']]) else mats['close'].loc[:end,p['sym']].dropna().iloc[-1]
                xp=raw*(1-cfg.slippage if p['side']=='long' else 1+cfg.slippage); pnl=p['qty']*((xp-p['entry']) if p['side']=='long' else (p['entry']-xp)); xf=p['qty']*xp*cfg.fee; cash+=pnl-xf; p.update(active=False,exit_date=min(nxt,end),exit=xp,reason='rebalance',exit_fee=xf)
            trades.append(p|{'reb':reb,'regime':reg})
        nav=cash; months.append((reb,reg,st,nav,nav/st-1,','.join(s for _,s in sel)))
    eqdf=pd.DataFrame(eq,columns=['date','equity']).drop_duplicates('date',keep='last').set_index('date').sort_index(); eqdf.loc[end,'equity']=nav
    dd=eqdf.equity/eqdf.equity.cummax()-1; yrs=(end-start).days/365.25; cagr=(nav/cfg.initial_nav)**(1/yrs)-1; dr=eqdf.equity.pct_change().dropna(); sh=float(dr.mean()/dr.std()*np.sqrt(365)) if dr.std()>0 else np.nan
    m=pd.DataFrame(months,columns=['reb','regime','start_nav','end_nav','ret','selected']); o=m[m.reb>=pd.Timestamp('2025-01-01')]; oos=float(o.end_nav.iloc[-1]/o.start_nav.iloc[0]-1) if len(o) else np.nan
    return {'final_nav':float(nav),'cagr':float(cagr),'max_drawdown':float(dd.min()),'max_drawdown_date':str(dd.idxmin().date()),'daily_sharpe':sh,'oos_return':oos,'funding_pnl':float(funding_total),'selected_short_symbols':sorted(selected_short_syms),'equity':eqdf,'monthly':m,'trades':pd.DataFrame(trades),'incidents':pd.DataFrame(incidents)}

def main():
    a=parse_args(); cfg=Config(start=a.start,end=a.end); root=Path(a.root); out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    print('Loading full PIT daily universe...'); df=load_daily(root,cfg); print('Rows',len(df),'symbols',df.symbol.nunique(),'date',df.date.min(),df.date.max())
    mats,r30,r90,vol,liq,src=matrices(df,cfg,a.exact_liquidity_csv); res=backtest(root,cfg,mats,r30,r90,vol,liq,use_pass_b=not a.disable_pass_b)
    res['equity'].to_csv(out/'equity_daily.csv'); res['monthly'].to_csv(out/'monthly.csv',index=False); res['trades'].to_csv(out/'trades.csv',index=False); res['incidents'].to_csv(out/'incidents.csv',index=False)
    summary={k:v for k,v in res.items() if k not in {'equity','monthly','trades','incidents'}}; summary['config']=asdict(cfg); summary['pit_rows']=int(len(df)); summary['pit_symbols']=int(df.symbol.nunique()); summary['liquidity_source']=src; summary['pass_b_enabled']=not a.disable_pass_b
    (out/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)); print(json.dumps(summary,indent=2,ensure_ascii=False))
if __name__=='__main__': main()
