"""
Orchestrator: iterate the indicator registry, fetch each dataset, save CSVs.

    python -m pipeline.run_pipeline

Dispatch is driven by Indicator.source (see pipeline/config.py):
  "oecd"    -> fetch_oecd_indicator       (generic SDMX)
  "statcan" -> fetch_statcan_indicator    (generic single-series table)
  "custom"  -> a bespoke function named by Indicator.fetch_fn

Robustness: each indicator is isolated. If a fetch returns empty / errors but a
previous CSV exists on disk, that CSV is preserved and the indicator is marked
STALE (the site keeps showing the last good data). The run only exits non-zero
on a hard failure — a dataset with no data and no prior CSV.
"""

import sys
import logging

from pipeline.config import INDICATORS
from pipeline.fetch_oecd import (fetch_oecd_indicator, fetch_labour_by_age,
                                 fetch_tax_structure, fetch_naturalisations, fetch_oda)
from pipeline.fetch_statcan import (
    fetch_statcan_indicator,
    fetch_population_quarterly, fetch_population_components, fetch_cpi,
    fetch_provincial_electricity, fetch_tuition, fetch_tuition_by_field,
    fetch_trade_us, fetch_cma_unemployment,
    fetch_age_structure, fetch_interprovincial_migration, fetch_minimum_wage,
    fetch_voter_turnout, fetch_cma_vacancy, fetch_income_distribution,
    fetch_piaac_skills,
    fetch_debt_service_ratio, fetch_provincial_finance, fetch_npr_by_type,
    fetch_median_income_by_family, fetch_tertiary_attainment, fetch_poverty_by_group,
    fetch_income_by_age, fetch_low_income_persistence, fetch_wages_by_province,
    fetch_cma_rent, fetch_wealth_by_age, fetch_npr_share, fetch_suicide_by_age,
    fetch_grads_by_field,
)
from pipeline.fetch_owid import (fetch_energy_mix, fetch_consumption_co2,
                                  fetch_co2_per_gdp, fetch_co2_global_context,
                                  fetch_life_expectancy_canada)
from pipeline.fetch_whr import fetch_happiness
from pipeline.fetch_worldbank import (fetch_worldbank_indicator, fetch_world_population,
                                       fetch_pm25_global_context, fetch_world_gdp,
                                       fetch_world_land_area)
from pipeline.fetch_boc import fetch_boc_indicator
from pipeline.fetch_geography import fetch_wildfire, fetch_sea_ice
from pipeline.fetch_environment import fetch_ghg, fetch_ghg_by_sector
from pipeline.fetch_air_quality import fetch_cesi_air_quality, fetch_apei_emissions
from pipeline.fetch_opioids import fetch_opioid_harms
from pipeline.fetch_climate import (
    fetch_cesi_temperature, fetch_ctvb_regional, fetch_city_temperatures,
    fetch_city_temperatures_seasonal, fetch_city_temperatures_monthly,
)
from pipeline.fetch_government import (
    fetch_govt_employment_by_level, fetch_public_sector_composition,
    fetch_fps_population, fetch_fps_by_department, fetch_fps_demographics,
    fetch_fps_executive, fetch_federal_finance_longrun, fetch_federal_expense_by_type,
    fetch_govt_spending_by_function, fetch_federal_spending_by_object,
    fetch_federal_spending_by_dept,
)
from pipeline.fetch_science import fetch_science_funding
from pipeline.fetch_innovation import (
    fetch_triadic_patents, fetch_rd_tax_support,
    fetch_industry_structure, fetch_foreign_rd_control, fetch_patents_us_owned,
)
from pipeline.fetch_immigration import (
    fetch_pr_origins, fetch_permit_origins, fetch_asylum_origins, fetch_asylum_outcomes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Bespoke fetchers referenced by Indicator.fetch_fn (source="custom").
CUSTOM_FETCHERS = {
    "fetch_population_quarterly": fetch_population_quarterly,
    "fetch_population_components": fetch_population_components,
    "fetch_cpi": fetch_cpi,
    "fetch_minimum_wage": fetch_minimum_wage,
    "fetch_voter_turnout": fetch_voter_turnout,
    "fetch_cma_vacancy": fetch_cma_vacancy,
    "fetch_income_distribution": fetch_income_distribution,
    "fetch_debt_service_ratio": fetch_debt_service_ratio,
    "fetch_piaac_skills": fetch_piaac_skills,
    "fetch_provincial_finance": fetch_provincial_finance,
    "fetch_npr_by_type": fetch_npr_by_type,
    "fetch_median_income_by_family": fetch_median_income_by_family,
    "fetch_tertiary_attainment": fetch_tertiary_attainment,
    "fetch_grads_by_field": fetch_grads_by_field,
    "fetch_poverty_by_group": fetch_poverty_by_group,
    "fetch_income_by_age": fetch_income_by_age,
    "fetch_low_income_persistence": fetch_low_income_persistence,
    "fetch_wages_by_province": fetch_wages_by_province,
    "fetch_cma_rent": fetch_cma_rent,
    "fetch_wealth_by_age": fetch_wealth_by_age,
    "fetch_npr_share": fetch_npr_share,
    "fetch_suicide_by_age": fetch_suicide_by_age,
    "fetch_energy_mix": fetch_energy_mix,
    "fetch_consumption_co2": fetch_consumption_co2,
    "fetch_life_expectancy_canada": fetch_life_expectancy_canada,
    "fetch_co2_per_gdp": fetch_co2_per_gdp,
    "fetch_co2_global_context": fetch_co2_global_context,
    "fetch_world_population": fetch_world_population,
    "fetch_world_gdp": fetch_world_gdp,
    "fetch_world_land_area": fetch_world_land_area,
    "fetch_pm25_global_context": fetch_pm25_global_context,
    "fetch_happiness": fetch_happiness,
    "fetch_provincial_electricity": fetch_provincial_electricity,
    "fetch_tuition": fetch_tuition,
    "fetch_tuition_by_field": fetch_tuition_by_field,
    "fetch_trade_us": fetch_trade_us,
    "fetch_cma_unemployment": fetch_cma_unemployment,
    "fetch_labour_by_age": fetch_labour_by_age,
    "fetch_tax_structure": fetch_tax_structure,
    "fetch_naturalisations": fetch_naturalisations,
    "fetch_oda": fetch_oda,
    "fetch_age_structure": fetch_age_structure,
    "fetch_interprovincial_migration": fetch_interprovincial_migration,
    "fetch_wildfire": fetch_wildfire,
    "fetch_sea_ice": fetch_sea_ice,
    "fetch_ghg": fetch_ghg,
    "fetch_ghg_by_sector": fetch_ghg_by_sector,
    "fetch_cesi_air_quality": fetch_cesi_air_quality,
    "fetch_apei_emissions": fetch_apei_emissions,
    "fetch_opioid_harms": fetch_opioid_harms,
    "fetch_cesi_temperature": fetch_cesi_temperature,
    "fetch_ctvb_regional": fetch_ctvb_regional,
    "fetch_city_temperatures": fetch_city_temperatures,
    "fetch_city_temperatures_seasonal": fetch_city_temperatures_seasonal,
    "fetch_city_temperatures_monthly": fetch_city_temperatures_monthly,
    # Government (workforce + federal spending)
    "fetch_govt_employment_by_level": fetch_govt_employment_by_level,
    "fetch_public_sector_composition": fetch_public_sector_composition,
    "fetch_fps_population": fetch_fps_population,
    "fetch_fps_by_department": fetch_fps_by_department,
    "fetch_fps_demographics": fetch_fps_demographics,
    "fetch_fps_executive": fetch_fps_executive,
    "fetch_federal_finance_longrun": fetch_federal_finance_longrun,
    "fetch_federal_expense_by_type": fetch_federal_expense_by_type,
    "fetch_govt_spending_by_function": fetch_govt_spending_by_function,
    "fetch_federal_spending_by_object": fetch_federal_spending_by_object,
    "fetch_federal_spending_by_dept": fetch_federal_spending_by_dept,
    # Science (government science funding)
    "fetch_science_funding": fetch_science_funding,
    # Innovation
    "fetch_triadic_patents": fetch_triadic_patents,
    "fetch_rd_tax_support": fetch_rd_tax_support,
    "fetch_industry_structure": fetch_industry_structure,
    "fetch_foreign_rd_control": fetch_foreign_rd_control,
    "fetch_patents_us_owned": fetch_patents_us_owned,
    # Immigration origins (IRCC open data)
    "fetch_pr_origins": fetch_pr_origins,
    "fetch_permit_origins": fetch_permit_origins,
    "fetch_asylum_origins": fetch_asylum_origins,
    "fetch_asylum_outcomes": fetch_asylum_outcomes,
}


def _run_one(ind):
    if ind.source == "oecd":
        return fetch_oecd_indicator(ind)
    if ind.source == "statcan":
        return fetch_statcan_indicator(ind)
    if ind.source == "worldbank":
        return fetch_worldbank_indicator(ind)
    if ind.source == "boc":
        return fetch_boc_indicator(ind)
    if ind.source == "custom":
        return CUSTOM_FETCHERS[ind.fetch_fn]()
    raise ValueError(f"Unknown source '{ind.source}' for indicator {ind.id}")


def run_all():
    results = {}
    for ind in INDICATORS:
        out_path = ind.out_path
        try:
            df = _run_one(ind)
            if df is None or len(df) == 0:
                if out_path.exists():
                    logger.warning(f"{ind.id}: empty fetch — keeping existing CSV (STALE)")
                    results[ind.id] = "STALE"
                else:
                    logger.error(f"{ind.id}: empty fetch and no prior CSV (FAILED)")
                    results[ind.id] = "FAILED"
            else:
                results[ind.id] = "OK"
        except Exception as e:
            logger.error(f"{ind.id}: {e}")
            results[ind.id] = "STALE" if out_path.exists() else "ERROR"

    # Summary
    logger.info("=" * 56)
    logger.info("PIPELINE SUMMARY")
    logger.info("=" * 56)
    for name, status in results.items():
        logger.info(f"  {status:7s} {name}")
    counts = {}
    for s in results.values():
        counts[s] = counts.get(s, 0) + 1
    logger.info(f"  totals: {counts}")

    stale = [n for n, s in results.items() if s == "STALE"]
    if stale:
        logger.warning(f"{len(stale)} dataset(s) kept stale (empty fetch): {stale}")

    # Hard failure only when a dataset has no data at all (no prior CSV).
    hard_fail = [n for n, s in results.items() if s in ("FAILED", "ERROR")]
    if hard_fail:
        logger.error(f"Hard failures (no data, no prior CSV): {hard_fail}")
        sys.exit(1)
    logger.info("Pipeline complete.")


if __name__ == "__main__":
    run_all()
