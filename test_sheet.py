import os
import sys
from datetime import date

def load_env():
    with open('.env') as f:
        for line in f:
            if line.strip() and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                os.environ[k] = v.strip('"\'')
load_env()

from src.google_sheets import get_runtime, CARD_COLS, next_free_row, parse_int

def main():
    ctx = get_runtime(date.today())
    day_ws = ctx.day_ws
    start_row = parse_int(ctx.config.get("tarjeta_start_row"), 8)
    end_row = parse_int(ctx.config.get("tarjeta_end_row"), 30)
    
    probe_col = CARD_COLS["importe"]
    cell_range = f"A{start_row}:H{start_row+3}"  # grab a few rows to see the headers
    values = day_ws.get(cell_range)
    print(f"Values in {cell_range}:")
    for i, row in enumerate(values):
        print(f"Row {start_row + i}: {row}")
        
if __name__ == "__main__":
    main()
