name: Bitget Scanner
on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch:

concurrency:
  group: bitget-scanner
  cancel-in-progress: false

jobs:
  pump:
    runs-on: ubuntu-latest
    timeout-minutes: 62
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests
      - name: restore state
        uses: actions/cache/restore@v4
        with:
          path: state-pump.json
          key: scanner-pump-${{ github.run_id }}
          restore-keys: scanner-pump-
      - name: Run pump tracker
        env:
          TG_TOKEN: ${{ secrets.TG_TOKEN }}
          TG_CHAT: ${{ secrets.TG_CHAT }}
          EMAIL_USER: ${{ secrets.EMAIL_USER }}
          EMAIL_PASS: ${{ secrets.EMAIL_PASS }}
          EMAIL_TO: ${{ secrets.EMAIL_TO }}
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
        run: python scanner.py --state state-pump.json --no-vol --loop 20 --duration 3000 --workers 12
      - name: save state
        uses: actions/cache/save@v4
        if: always()
        with:
          path: state-pump.json
          key: scanner-pump-${{ github.run_id }}

  volume:
    runs-on: ubuntu-latest
    timeout-minutes: 62
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests
      - name: restore state
        uses: actions/cache/restore@v4
        with:
          path: state-vol.json
          key: scanner-vol-${{ github.run_id }}
          restore-keys: scanner-vol-
      - name: Run volume scanner
        env:
          TG_TOKEN: ${{ secrets.TG_TOKEN }}
          TG_CHAT: ${{ secrets.TG_CHAT }}
          EMAIL_USER: ${{ secrets.EMAIL_USER }}
          EMAIL_PASS: ${{ secrets.EMAIL_PASS }}
          EMAIL_TO: ${{ secrets.EMAIL_TO }}
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
        run: python scanner.py --state state-vol.json --no-pump --loop 30 --duration 3000 --workers 12
      - name: save state
        uses: actions/cache/save@v4
        if: always()
        with:
          path: state-vol.json
          key: scanner-vol-${{ github.run_id }}
