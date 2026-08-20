"""Redirected: use scorecard.py (live trades table, stored qty)."""
from scorecard import print_scorecard

if __name__ == "__main__":
    print("generate_summary.py now delegates to scorecard.py")
    print_scorecard()
