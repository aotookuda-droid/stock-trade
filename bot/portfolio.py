class Portfolio:
    def __init__(self):
        self.positions = {}
        self.trades = []

    def add_position(self, symbol, quantity, price):
        self.positions[symbol] = self.positions.get(symbol, 0) + quantity
        self.trades.append({'symbol': symbol, 'quantity': quantity, 'price': price})

    def sell_position(self, symbol, quantity, price):
        if symbol in self.positions and self.positions[symbol] >= quantity:
            self.positions[symbol] -= quantity
            self.trades.append({'symbol': symbol, 'quantity': -quantity, 'price': price})
        else:
            raise ValueError('Not enough quantity to sell.')

    def calculate_portfolio_value(self, current_prices):
        total_value = 0.0
        for symbol, quantity in self.positions.items():
            total_value += quantity * current_prices.get(symbol, 0)
        return total_value

    def get_statistics(self):
        total_trades = len(self.trades)
        total_positions = len(self.positions)
        return {'total_trades': total_trades, 'total_positions': total_positions}

# Example usage
if __name__ == '__main__':
    portfolio = Portfolio()
    portfolio.add_position('AAPL', 10, 150.00)
    portfolio.sell_position('AAPL', 5, 155.00)
    print('Portfolio Value:', portfolio.calculate_portfolio_value({'AAPL': 157.00}))
    print('Statistics:', portfolio.get_statistics())
