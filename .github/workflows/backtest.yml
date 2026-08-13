name: Run RSind backtest

# Manual only — this is a heavy job (downloads ~5 years of bars for the whole
# NSE list) and has nothing to do with the daily ranking update.
on:
  workflow_dispatch:
    inputs:
      years:
        description: "Years of trading to test"
        default: "3"
      rs_trigger:
        description: "RS percentile the stock must cross above"
        default: "80"
      stop_pct:
        description: "Initial stop, % below entry"
        default: "5"
      atr_mult:
        description: "Move stop to breakeven after this many ATRs of profit"
        default: "1"
      min_mcap_cr:
        description: "Minimum market cap, Rs crore"
        default: "2000"
      min_turnover_cr:
        description: "Minimum median 50-day turnover, Rs crore"
        default: "5"
      capital:
        description: "Rupees per position"
        default: "100000"

permissions:
  contents: write

concurrency:
  group: rsind-backtest
  cancel-in-progress: false

jobs:
  backtest:
    runs-on: ubuntu-latest
    timeout-minutes: 180
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run backtest
        run: |
          python backtest.py \
            --years "${{ inputs.years }}" \
            --rs-trigger "${{ inputs.rs_trigger }}" \
            --stop-pct "${{ inputs.stop_pct }}" \
            --atr-mult "${{ inputs.atr_mult }}" \
            --min-mcap-cr "${{ inputs.min_mcap_cr }}" \
            --min-turnover-cr "${{ inputs.min_turnover_cr }}" \
            --capital "${{ inputs.capital }}"

      - name: Show summary in the run log
        if: always()
        run: |
          if [ -f output/backtest/summary.md ]; then
            cat output/backtest/summary.md >> "$GITHUB_STEP_SUMMARY"
          fi

      - name: Commit results
        run: |
          git config user.name "rsind-bot"
          git config user.email "actions@users.noreply.github.com"
          git add output/backtest/
          git diff --cached --quiet && echo "No changes to commit." && exit 0
          git commit -m "Backtest: RS>${{ inputs.rs_trigger }}, ${{ inputs.stop_pct }}% stop, ${{ inputs.years }}y"
          for i in 1 2 3; do
            git pull --rebase --autostash origin main && git push && exit 0
            echo "Push attempt $i failed, retrying..."
            sleep 5
          done
          echo "Could not push after 3 attempts." && exit 1
