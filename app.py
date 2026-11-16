"""International cost of capital calculator."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
import re

import pandas as pd
import pycountry
import requests
import streamlit as st
from bs4 import BeautifulSoup
from babel.numbers import get_territory_currencies


BETA_XLS = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
BETA_HTML = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html"
SPREAD = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ratings.html"
OECD = "https://www.oecd.org/en/data/datasets/corporate-income-tax-rates-database.html"
OECD_API = "https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,2.0/"
BONDS = "https://tradingeconomics.com/bonds"
SOFTWARE = "Software (System & Application)"
MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
NAMES = {
    "GBR": "United Kingdom", "USA": "United States", "KOR": "South Korea",
    "PRK": "North Korea", "RUS": "Russia", "CZE": "Czech Republic",
    "TWN": "Taiwan", "VNM": "Vietnam", "TUR": "Turkey",
    "BOL": "Bolivia", "VEN": "Venezuela", "IRN": "Iran",
}


def calculate(beta, tax, debt, erp, usd_yield, local_yield, usd_spread, local_override=None):
    """Rates are decimal fractions. Return USD and local results plus the levered beta."""
    levered = beta * (1 + (1 - tax) * debt / (1 - debt))
    # Preserve the ratio of all-in borrowing to government gross yields.
    auto = usd_spread * (1 + local_yield) / (1 + usd_yield)
    local_spread = auto if local_override is None else local_override

    def costs(yield_, spread):
        equity = yield_ + levered * erp
        borrowing = yield_ + spread
        after_tax = borrowing * (1 - tax)
        return {
            "Government yield": yield_, "BBB spread": spread,
            "Cost of equity": equity, "Pre-tax cost of debt": borrowing,
            "After-tax cost of debt": after_tax,
            "WACC": (1 - debt) * equity + debt * after_tax,
        }

    return costs(usd_yield, usd_spread), costs(local_yield, local_spread), levered, auto


def get(url, **kwargs):
    response = requests.get(url, timeout=(4, 8), headers={
        "User-Agent": "Mozilla/5.0 (compatible; CapitalCostResearch/1.0)",
        **kwargs.pop("headers", {}),
    }, **kwargs)
    response.raise_for_status()
    return response


def analysis_date(text):
    match = re.search(rf"(?:as of|updated in)\s+({MONTH}\s+\d{{4}})", text, re.I)
    return match.group(1) if match else "Source date unavailable"


def normalized_label(value):
    return " ".join(str(value).split()).casefold()


def parse_betas(frame):
    for index, row in frame.iterrows():
        names = [normalized_label(cell) for cell in row]
        if "industry name" not in names or "unlevered beta" not in names:
            continue
        fields = frame.iloc[index + 1:, [names.index("industry name"), names.index("unlevered beta")]].copy()
        fields.columns = ["industry", "beta"]
        fields["beta"] = pd.to_numeric(fields["beta"], errors="coerce")
        values = {}
        for industry, beta in fields.dropna().itertuples(index=False, name=None):
            industry = " ".join(str(industry).split())
            if "software" in industry.casefold() and "system" in industry.casefold() and "application" in industry.casefold():
                industry = SOFTWARE
            values[industry] = beta
        if SOFTWARE in values:
            return values
    raise ValueError("Could not find the software industry in the unlevered beta table")


def fetch_betas():
    try:
        sheets = pd.read_excel(BytesIO(get(BETA_XLS).content), sheet_name=None, header=None, engine="xlrd")
        for frame in sheets.values():
            try:
                values = parse_betas(frame)
            except ValueError:
                continue
            stamp = analysis_date(" ".join(frame.head(8).fillna("").astype(str).to_numpy().ravel()))
            return values, stamp, BETA_XLS
        raise ValueError("Industry beta workbook unavailable")
    except Exception:
        html = BeautifulSoup(get(BETA_HTML).text, "html.parser")
        rows = [
            [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]
            for row in html.select("tr")
        ]
        return parse_betas(pd.DataFrame(rows)), analysis_date(html.get_text(" ", strip=True)), BETA_HTML


def fetch_spread():
    html = BeautifulSoup(get(SPREAD).text, "html.parser")
    for row in html.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td", recursive=False)]
        for index, cell in enumerate(cells[:-1]):
            if cell.replace(" ", "") == "Baa2/BBB":
                spread = float(cells[index + 1].rstrip("%"))
                return spread, analysis_date(html.get_text(" ", strip=True)), SPREAD
    raise ValueError("BBB row unavailable")


def fetch_tax(code):
    response = get(OECD_API + f"{code}.A..ST..S13...", params={
        "startPeriod": str(date.today().year - 8),
        "dimensionAtObservation": "AllDimensions",
    }, headers={"Accept": "application/vnd.sdmx.data+csv;version=2;file=true;labels=name"})
    frame = pd.read_csv(StringIO(response.text))
    label_columns = frame.select_dtypes(include=["object", "string"]).columns
    match = pd.Series(False, index=frame.index)
    for column in label_columns:
        match |= frame[column].astype(str).str.contains("combined corporate income tax rate", case=False, regex=False)
    values = frame.loc[match].dropna(subset=["OBS_VALUE", "TIME_PERIOD"]).sort_values("TIME_PERIOD")
    if values.empty:
        raise ValueError("No OECD combined statutory rate for this country")
    row = values.iloc[-1]
    return float(row["OBS_VALUE"]), str(int(row["TIME_PERIOD"])), OECD


def country_name(country):
    return NAMES.get(country.alpha_3, getattr(country, "common_name", country.name))


def fetch_yield(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    url = f"https://tradingeconomics.com/{slug}/government-bond-yield"
    html = BeautifulSoup(get(url).text, "html.parser")
    source = " ".join([tag.get("content", "") for tag in html.select("meta[name=description],meta[property='og:description']")])
    source += " " + html.get_text(" ", strip=True)
    pattern = rf"(-?\d+(?:\.\d+)?)\s*(?:%|percent)\s+on\s+({MONTH}\s+\d{{1,2}},?\s+\d{{4}})"
    for match in re.finditer(pattern, source, re.I):
        preceding = source[max(0, match.start() - 160):match.start()].lower()
        if "yield" in preceding and "forecast" not in preceding[-55:]:
            return float(match.group(1)), match.group(2), url
    raise ValueError("Trading Economics did not expose a dated 10-year quote")


def fetch_with_backup(key, argument):
    loaders = {"betas": fetch_betas, "spread": fetch_spread, "tax": fetch_tax, "yield": fetch_yield}
    try:
        args = () if argument is None else (argument,)
        value, stamp, url = loaders[key](*args)
        return value, stamp, url, "Fetched " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (requests.RequestException, ValueError, KeyError, ImportError, IndexError) as exc:
        backups = {
            ("betas", None): (
                pd.read_csv(Path(__file__).with_name("betas_snapshot.csv")).set_index("industry")["beta"].to_dict(),
                "January 2026", BETA_HTML,
            ),
            ("spread", None): (1.11, "January 2026", SPREAD),
            ("tax", "USA"): (25.57, "2024", OECD),
            ("tax", "GBR"): (25.00, "2024", OECD),
            ("yield", "United States"): (5.14, "November 16, 2026", "https://tradingeconomics.com/united-states/government-bond-yield"),
            ("yield", "United Kingdom"): (5.35, "November 16, 2026", "https://tradingeconomics.com/united-kingdom/government-bond-yield"),
        }
        value, stamp, url = backups.get((key, argument), (None, "Unavailable", {"tax": OECD, "yield": BONDS}.get(key, BETA_HTML)))
        reason = str(exc).splitlines()[0][:135] if str(exc) else type(exc).__name__
        status = "Dated snapshot" if value is not None else "Manual input needed"
        return value, stamp, url, f"{status} | {reason}"


cached_fetch = st.cache_data(ttl=3600, show_spinner=False)(fetch_with_backup)


def override(label, fetched, key, low, high, step):
    enabled = st.checkbox(f"Enter {label} manually", value=fetched is None, key=f"{key}_on")
    if enabled:
        return st.number_input(label, min_value=low, max_value=high, value=fetched,
                               step=step, key=key, placeholder="Enter a value")
    return fetched


def main():
    st.set_page_config(page_title="International Cost of Capital", layout="wide")
    st.title("International Cost of Capital")
    st.caption("Compare the same project in U.S. dollars and the selected country's currency.")
    countries = {country_name(c): c.alpha_3 for c in pycountry.countries}
    with st.sidebar:
        country = st.selectbox("Target country", ["United States", *sorted(set(countries) - {"United States"})])
        tax_basis = st.radio("Project tax rate", ["U.S.", "Target country"], horizontal=True)
        erp = st.number_input("Equity risk premium (%)", 0.0, 100.0, 6.5, .1) / 100
        debt = st.slider("Debt financing (%)", 0, 99, 30) / 100
        if st.button("Refresh source data"):
            cached_fetch.clear()

    jobs = [("betas", None), ("spread", None), ("tax", "USA"), ("yield", "United States")]
    if country != "United States":
        jobs.extend([("tax", countries[country]), ("yield", country)])
    with st.spinner("Loading data sources…"):
        # The cache does not depend on scenario controls.
        with ThreadPoolExecutor(max_workers=6) as pool:
            data = list(pool.map(lambda job: cached_fetch(*job), jobs))

    beta_source, spread_source, us_tax, us_yield = data[:4]
    local_tax, local_yield = data[4:] if len(data) > 4 else (us_tax, us_yield)
    betas = beta_source[0]
    names = [SOFTWARE, *sorted(set(betas) - {SOFTWARE})]
    territory = pycountry.countries.get(alpha_3=countries[country]).alpha_2
    currencies = get_territory_currencies(territory, tender=True)
    currency_codes = " / ".join(currencies)
    local_label = f"{country} · {currency_codes}" if currencies else f"{country} currency"
    with st.sidebar:
        industry = st.selectbox("Industry", names)
    tax_source = us_tax if tax_basis == "U.S." else local_tax
    with st.expander("Manual data inputs", expanded=any(x is None for x in [tax_source[0], us_yield[0], local_yield[0]])):
        left, right = st.columns(2)
        with left:
            beta = override("unlevered beta", betas[industry], f"beta_{industry}", -10., 20., .01)
            tax = override("project corporate tax rate (%)", tax_source[0],
                           f"tax_{country}_{tax_basis}", 0., 100., .01)
            usd_spread = override("USD BBB spread (%)", spread_source[0], "usd_spread", 0., 100., .01)
        with right:
            uy = override("U.S. 10-year yield (%)", us_yield[0], "us_yield", -99., 250., .01)
            ly = override("target-country 10-year yield (%)", uy if country == "United States" else local_yield[0],
                          f"target_{country}", -99., 250., .01)
            auto_pc = usd_spread * (100 + ly) / (100 + uy) if None not in (usd_spread, ly, uy) else None
            if auto_pc is not None:
                st.caption(f"Automatic local BBB spread · {auto_pc:.3f}%")
            manual_local = st.checkbox("Enter local BBB spread manually")
            ls = st.number_input("Local BBB spread (%)", 0., 1000., auto_pc, .01) if manual_local else None

    with st.expander("Sources and dates"):
        sources = [("U.S. unlevered industry betas", beta_source), ("U.S. BBB spread", spread_source),
                   ("U.S. corporate tax", us_tax), ("U.S. 10-year yield", us_yield),
                   (f"{country} corporate tax", local_tax), (f"{country} 10-year yield", local_yield)]
        for label, (value, stamp, url, status) in sources:
            display = f" · {value:.4g}%" if isinstance(value, (float, int)) else ""
            st.markdown(f"[{label}]({url}){display} · {stamp} · {status}")
    if any(row[-1][3].startswith(("Dated", "Manual")) for row in sources):
        st.warning("Some live sources are unavailable. See Sources and dates for snapshots and missing inputs.")
    if None in (beta, tax, usd_spread, uy, ly) or (manual_local and ls is None):
        st.info("Enter the missing values in Manual data inputs to calculate results.")
        st.stop()

    scenario = (tax / 100, debt, erp, uy / 100, ly / 100, usd_spread / 100, None if ls is None else ls / 100)
    usd, local, levered, _ = calculate(beta, *scenario)
    st.subheader(industry)
    st.caption(f"Project tax {tax:.2f}% ({tax_basis}) · Debt share {debt:.0%} · Levered beta {levered:.3f}")
    left, right = st.columns(2)
    for column, title, results in [(left, "USD", usd), (right, local_label, local)]:
        with column:
            st.markdown(f"### {title}")
            st.metric("WACC", f"{results['WACC']:.2%}")
            st.dataframe(pd.DataFrame({"Measure": list(results)[:-1],
                                       "Rate": [f"{v:.2%}" for v in list(results.values())[:-1]]}),
                         hide_index=True, use_container_width=True)
    rows = []
    for sector in names:
        unlevered = beta if sector == industry else betas[sector]
        dollars, foreign, b, _ = calculate(unlevered, *scenario)
        rows.append({"Industry": sector, "Unlevered beta": unlevered, "Levered beta": b,
                     **{f"USD {k} (%)": 100 * v for k, v in dollars.items()},
                     **{f"Local {k} (%)": 100 * v for k, v in foreign.items()},
                     "Target country": country, "Local currency": currency_codes, "Tax basis": tax_basis, "Tax (%)": tax,
                     "Debt share (%)": 100 * debt, "ERP (%)": 100 * erp})
    comparison = pd.DataFrame(rows)
    st.subheader("All-industry comparison")
    st.dataframe(comparison.round(3), hide_index=True, use_container_width=True)
    st.download_button("Download results CSV", comparison.to_csv(index=False).encode(),
                       "international_cost_of_capital.csv", "text/csv")
    with st.expander("Model assumptions"):
        st.markdown(
            "Both currencies use the same project tax rate, debt share, and equity risk premium. "
            "The project beta is relevered using debt/equity = debt share ÷ equity share. "
            "Cost of equity uses the local government yield plus levered beta × equity risk premium. "
            "Pre-tax cost of debt is the government yield plus the BBB spread. "
            "WACC weights cost of equity and after-tax cost of debt by their financing shares. "
            "The automatic local BBB spread scales the USD spread by (1 + local yield) ÷ (1 + U.S. yield). "
            "This gross-rate conversion is an assumption pending confirmation against the course formula."
        )


if __name__ == "__main__":
    main()
