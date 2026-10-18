import importlib.util
import os
import pickle
import sys

sys.path.insert(0, 'E:/StockAgent')

spec = importlib.util.spec_from_file_location(
    'lp', 'E:/StockAgent/research/05_lowpos/31_lowpos_portfolio.py')
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)

signals = pickle.load(open(lp.SIG, 'rb'))
book = lp.PriceBook(lp.RAW)
extra = pickle.load(open(lp.EXTRA, 'rb'))

FILES = {
    'ma20': 'index_sh_full.json', 'sh_ma20': 'index_sh_full.json',
    'zz500_ma20': 'index_sh000905.json', 'zz1000_ma20': 'index_sh000852.json',
    'gz2000_ma20': 'index_sz399303.json', 'none': 'index_sh_full.json',
    'pullback': 'index_sh_full.json', 'below_ma20': 'index_sh_full.json',
}
MD = {k: lp.load_market_dates(os.path.join(lp.ROOT, '.data', v), k) for k, v in FILES.items()}

BASE = dict(mode='early', flavor='mild', hold=20, stop=0.18, target=0.30,
            trail=0.0, trend_ma10=False, market_filter='ma20')

VARIANTS = {
    '00 base': dict(),
    '01 mcap<=50': dict(max_mcap=50),
    '02 mcap<=100': dict(max_mcap=100),
    '03 price<=20': dict(max_price=20),
    '04 price<=10': dict(max_price=10),
    '05 amount<=2': dict(max_amount=2),
    '06 amount<=1': dict(max_amount=1),
    '07 gap<=3%': dict(gap_max=0.03),
    '08 rank mild_mcap': dict(flavor='mild_mcap'),
    '09 zz500_ma20': dict(market_filter='zz500_ma20'),
    '10 zz1000_ma20': dict(market_filter='zz1000_ma20'),
    '11 gz2000_ma20': dict(market_filter='gz2000_ma20'),
    '12 pullback': dict(market_filter='pullback'),
    '13 none': dict(market_filter='none'),
    '14 below_ma20': dict(market_filter='below_ma20'),
    '15 maxpos4': dict(max_pos=4),
    '16 maxpos3': dict(max_pos=3),
    '17 target40': dict(target=0.40),
    '18 target25': dict(target=0.25),
    '19 stop0': dict(stop=0.0),
    '20 stop20': dict(stop=0.20),
    '21 hold15': dict(hold=15),
    '22 hold25': dict(hold=25),
    '23 mcap50+price20': dict(max_mcap=50, max_price=20),
    '24 mcap100+price20': dict(max_mcap=100, max_price=20),
    '25 mcap50+amount2': dict(max_mcap=50, max_amount=2),
}

END_TEST = '2024-01-01'
print(f"{'变体':<20}{'交易':>6}{'期末':>12}{'收益%':>9}{'年化%':>8}{'回撤%':>8}{'胜率%':>7}{'PF':>6}{'2024-26%':>10}")
for name, over in VARIANTS.items():
    kw = dict(BASE)
    kw.update(over)
    mf = kw.pop('market_filter')
    r = lp.simulate(signals, book, capital=300000, max_pos=kw.pop('max_pos', 5),
                    start='2021-01-04', end='2026-10-16', market_dates=MD[mf],
                    extra=extra, **kw)
    # test only
    r2 = lp.simulate(signals, book, capital=300000, max_pos=5,
                     start=END_TEST, end='2026-10-16', market_dates=MD[mf],
                     extra=extra, **{k: v for k, v in kw.items() if k != 'max_pos'})
    print(f"{name:<20}{r['n_trades']:>6}{r['final']:>12,.0f}{r['return_pct']:>9.1f}"
          f"{r['cagr']:>8.1f}{r['mdd']:>8.1f}{r['win_rate']:>7.1f}{r['profit_factor']:>6.2f}"
          f"{r2['return_pct']:>10.1f}")
