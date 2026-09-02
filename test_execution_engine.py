#!/usr/bin/env python3
from __future__ import annotations
import tempfile
from decimal import Decimal
from pathlib import Path

from bybit_execution_engine import D, EngineConfig, ExecutionEngine, ProtectedOrderResult

class FakeClient:
    def __init__(self, market='100', min_notional='5', fill=True, partial=False):
        self.market=D(market); self.min_notional=D(min_notional); self.fill=fill; self.partial=partial
        self.created=[]; self.stops=[]; self._orders={}
    def server_time(self): return {'time':123}
    def ticker(self,symbol,category=None): return {'lastPrice':str(self.market),'ask1Price':str(self.market),'bid1Price':str(self.market)}
    def instrument(self,symbol,category=None):
        return {'status':'Trading','priceFilter':{'tickSize':'0.1'},'lotSizeFilter':{'qtyStep':'0.01','minOrderQty':'0.01','minNotionalValue':str(self.min_notional)}}
    def position(self,symbol,category=None): return []
    def open_orders(self,symbol,category=None,order_link_id=None):
        o=self._orders.get(order_link_id)
        return [o] if o else []
    def order_history(self,symbol,category=None,order_link_id=None):
        o=self._orders.get(order_link_id)
        return [o] if o else []
    def executions(self,symbol,category=None,order_link_id=None):
        o=self._orders.get(order_link_id)
        if not o or o['orderStatus'] not in {'Filled','PartiallyFilledCanceled'}: return []
        q=D(o['qty']); fq=q/2 if self.partial else q
        return [{'execQty':str(fq),'execPrice':str(self.market)}]
    def create_order(self,payload):
        self.created.append(payload)
        status='PartiallyFilledCanceled' if self.partial else ('Filled' if self.fill else 'Cancelled')
        self._orders[payload['orderLinkId']]={'orderId':'OID1','orderLinkId':payload['orderLinkId'],'orderStatus':status,'qty':payload['qty']}
        return {'result':{'orderId':'OID1','orderLinkId':payload['orderLinkId']}}
    def set_trading_stop(self,symbol,stop_loss,category='linear',position_idx=0):
        self.stops.append((symbol,stop_loss)); return {'result':{}}

def eng(fake,mode='testnet'):
    td=tempfile.TemporaryDirectory(); cfg=EngineConfig(mode=mode,journal_path=str(Path(td.name)/'j.jsonl'),reconcile_timeout_sec=.01,poll_sec=.001)
    e=ExecutionEngine(cfg,client=fake); e._td=td; return e

def test_drift_blocks():
    e=eng(FakeClient(market='100.2'))
    r=e.protected_ioc('XUSDT','Buy',D('10'),D('100'),'id-drift')
    assert r.status=='SKIPPED_DRIFT'
    assert not e.client.created

def test_min_notional_blocks():
    e=eng(FakeClient(market='100',min_notional='7'))
    r=e.protected_ioc('XUSDT','Buy',D('6'),D('100'),'id-min')
    assert r.status=='SKIPPED_MIN_ORDER'
    assert not e.client.created

def test_limit_ioc_cap():
    e=eng(FakeClient(market='100'))
    r=e.protected_ioc('XUSDT','Buy',D('10'),D('100'),'id-cap')
    assert r.status=='FILLED'
    assert D(e.client.created[0]['price']) <= D('100.15')
    assert e.client.created[0]['timeInForce']=='IOC'

def test_partial_fill_actual_qty_and_stop():
    e=eng(FakeClient(market='100',partial=True))
    r=e.protected_ioc('XUSDT','Sell',D('10'),D('100'),'id-part',D('.25'))
    assert r.status=='FILLED'
    assert D(r.filled_qty)>0 and D(r.filled_qty)<D(r.requested_qty)
    assert e.client.stops
    assert D(r.stop_loss)>=D('125')

def test_idempotent_same_orderlinkid():
    e=eng(FakeClient(market='100'))
    r1=e.protected_ioc('XUSDT','Buy',D('10'),D('100'),'same-id')
    n=len(e.client.created)
    r2=e.protected_ioc('XUSDT','Buy',D('10'),D('100'),'same-id')
    assert r1.status=='FILLED'
    assert r2.status=='ALREADY_SUBMITTED'
    assert len(e.client.created)==n

def test_sell_limit_is_floor_not_worse():
    e=eng(FakeClient(market='100'))
    r=e.protected_ioc('XUSDT','Sell',D('10'),D('100'),'sell-cap')
    assert D(e.client.created[0]['price']) >= D('99.85')

def run():
    tests=[v for k,v in globals().items() if k.startswith('test_') and callable(v)]
    for t in tests:t(); print('PASS',t.__name__)
    print(f'{len(tests)}/{len(tests)} PASS')
if __name__=='__main__':run()
