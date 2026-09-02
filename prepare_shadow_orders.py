#!/usr/bin/env python3
"""Enrich forward targets with execution-time reference and tranche notional.

For shadow execution the reference is the immediately observed Bybit touch before
submitting the protected IOC. This deliberately measures infrastructure delay and
execution drift rather than pretending the UTC D1 open was still available.
"""
from __future__ import annotations
import argparse, json, os
from decimal import Decimal
from pathlib import Path
from bybit_execution_engine import BybitREST, D, EngineConfig


def main():
    p=argparse.ArgumentParser(); p.add_argument('--input',default='signal.json'); p.add_argument('--output',default='signal.prepared.json'); p.add_argument('--shadow-nav',default=os.getenv('SHADOW_NAV_USDT','100'))
    a=p.parse_args(); x=json.loads(Path(a.input).read_text()); nav=D(a.shadow_nav); client=BybitREST(EngineConfig(mode='dry-run'))
    per_tranche=nav*D('0.20')/D('3')
    errs=[]
    for t in x.get('targets',[]):
        if not t.get('execute_today'): continue
        try:
            q=client.ticker(t['symbol']); side=t.get('side')
            ref=D(q.get('ask1Price') or q.get('lastPrice')) if side=='long' else D(q.get('bid1Price') or q.get('lastPrice'))
            t['reference_price']=str(ref); t['quote_notional']=str(per_tranche)
            t['reference_source']='bybit_touch_immediately_before_shadow_ioc'
        except Exception as e:
            errs.append({'symbol':t.get('symbol'),'error':repr(e)})
    x.setdefault('adapter_meta',{})['prepare_errors']=errs
    if errs: x['data_fresh']=False
    Path(a.output).write_text(json.dumps(x,indent=2)); print(json.dumps(x,indent=2))
if __name__=='__main__':main()
