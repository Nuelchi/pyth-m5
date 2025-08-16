import backtrader as bt


class ExampleStrategy(bt.Strategy):
	params = dict(
		fast=10,
		slow=20,
	)

	def __init__(self):
		self.smas = {d: bt.ind.SMA(d.close, period=self.p.fast) for d in self.datas}
		self.smal = {d: bt.ind.SMA(d.close, period=self.p.slow) for d in self.datas}

	def next(self):
		for d in self.datas:
			pos = self.getposition(d)
			if not pos and self.smas[d][0] > self.smal[d][0]:
				self.buy(data=d)
			elif pos and self.smas[d][0] < self.smal[d][0]:
				self.close(data=d)
