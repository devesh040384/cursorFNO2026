"""Redirected: use scorecard.py (live trades table, stored qty)."""
from scorecard import print_scorecard

if __name__ == "__main__":
    print("performance_reports.py now delegates to scorecard.py")
    print_scorecard(show_all=True)
