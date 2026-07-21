# Seasonality Engine

## Seasonal mechanisms

- **Weather activation:** heat, cold, rain, snow, wind, daylight or water temperature.
- **Calendar activation:** holidays, school schedules, hunting/fishing periods, vacations and event seasons.
- **Lifecycle activation:** opening/closing a cottage, winterizing, garden planting, preseason training.
- **Participation activation:** tournaments, races, migration periods and recreation peaks.
- **Retail activation:** gifting, clearance, replenishment and pre-season buying.
- **Trend activation:** short-lived creative or cultural attention.

## Required fields

Each signal records region, hemisphere, start/end window, evidence year, signal type, lead time, confidence and next verification date.

## Timing model

Research and sourcing happen before use. Define:

- `use_window_start`
- `purchase_lead_days`
- `content_lead_days`
- `supplier_lead_days`
- `decision_date = use_window_start - max(leads)`

## Baseline versus current conditions

Climate normals support expected timing. Current forecasts and observed conditions support tactical timing. Never represent a normal as a forecast or a single abnormal season as the baseline.

## Seasonal opportunity types

1. predictable evergreen cycle;
2. regionally staggered cycle;
3. weather-triggered surge;
4. event-triggered spike;
5. short creative trend;
6. counter-seasonal clearance or preparation opportunity.

A product may be evergreen while its messaging is seasonal.
