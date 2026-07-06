import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--date", required=True)
args = parser.parse_args()
print(f"Hello from RunRail for {args.date}")

