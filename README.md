# International Cost of Capital

A Streamlit app for comparing a project's annual nominal equity cost, debt cost,
and WACC in USD and the target country's currency.

## Run locally

1. Install Python 3.11 or 3.12.
2. Run `python -m pip install -r requirements.txt`.
3. Run `python -m streamlit run app.py`.

## Deploy

Upload app.py, requirements.txt, betas_snapshot.csv and README.md to the root of
a GitHub repository. In [Streamlit Community Cloud](https://share.streamlit.io/),
choose Create app, select the repository and branch, set the main file path to
app.py, and deploy. Open the published URL to confirm the default scenario loads.

## Data and calculations

The default scenario is Software (System & Application), United States, U.S. tax,
6.5% equity risk premium and 30% debt. The source panel distinguishes live fetches,
dated snapshots and missing values. Refresh clears the one-hour cache. The app
reads Damodaran's U.S. XLS with an HTML fallback, Damodaran's large non-financial
BBB spread, the OECD combined statutory tax series and each Trading Economics
10-year government bond page. Countries without data can be entered manually.
The all-industry export uses the fetched or bundled sector list and applies a
manual beta override to the selected industry only.

The model uses a shared tax rate, D/V, and equity risk premium.

- Levered beta = unlevered beta × (1 + (1 − tax) × D/E).
- Cost of equity = government yield + levered beta × equity risk premium.
- Pre-tax debt cost = government yield + borrowing spread.
- WACC = (1 − D/V) × equity cost + D/V × pre-tax debt cost × (1 − tax).
- Automatic local spread = USD spread × (1 + local yield) / (1 + USD yield).

The last expression is a gross-rate conversion assumption. Compare it with the
formula provided in class. The local spread field also accepts a manual override.
