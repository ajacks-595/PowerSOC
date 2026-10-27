# PowerSOC integrations

PowerSOC exposes a **token-gated, read-only** machine API so other tools can
consume its data. Generate a token in **Settings → Home API** (it's shown once);
send it as `X-PowerSOC-Token: <token>` or `Authorization: Bearer <token>`.

Endpoints (`/api/home/*`): `live`, `today`, `day/<YYYY-MM-DD>`, `history`,
`tariffs`, `weather`, `forecast`, `carbon`, `plan`, `reconcile`, `account`,
`status`. Each returns the standard `{success, data, error}` envelope. There is
also a Prometheus `/metrics` endpoint (same token).

`account` returns the Octopus balance, a bill forecast (month-to-date net cost
projected to month end), and upcoming Octoplus Saving Sessions — useful for an
HA automation that pre-charges the battery before a session.

## Home Assistant (REST sensors)

`configuration.yaml` — exposes live power + today's totals as `sensor.powersoc_*`
suitable for the HA Energy dashboard:

```yaml
rest:
  - resource: "http://192.168.1.50:8073/api/home/live"
    headers:
      X-PowerSOC-Token: !secret powersoc_token
    scan_interval: 60
    sensor:
      - name: "PowerSOC PV Power"
        value_template: "{{ value_json.data.sunsynk.pv_w | default(0) }}"
        unit_of_measurement: "W"
        device_class: power
      - name: "PowerSOC Battery SoC"
        value_template: "{{ value_json.data.sunsynk.soc_pct | default(0) }}"
        unit_of_measurement: "%"
        device_class: battery
      - name: "PowerSOC Grid Power"
        value_template: "{{ value_json.data.sunsynk.grid_w | default(0) }}"
        unit_of_measurement: "W"
        device_class: power
      - name: "PowerSOC House Load"
        value_template: "{{ value_json.data.sunsynk.load_w | default(0) }}"
        unit_of_measurement: "W"
        device_class: power
      - name: "PowerSOC Battery Power"
        value_template: "{{ value_json.data.sunsynk.battery_w | default(0) }}"
        unit_of_measurement: "W"
        device_class: power
  - resource: "http://192.168.1.50:8073/api/home/today"
    headers:
      X-PowerSOC-Token: !secret powersoc_token
    scan_interval: 600
    sensor:
      - name: "PowerSOC Solar Today"
        value_template: "{{ value_json.data.summary.pv_kwh | default(0) }}"
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: total_increasing
      - name: "PowerSOC Net Cost Today"
        value_template: "{{ (value_json.data.summary.net_cost_pence | default(0) | float / 100) | round(2) }}"
        unit_of_measurement: "GBP"
        device_class: monetary
      - name: "PowerSOC Self-Sufficiency Today"
        value_template: "{{ value_json.data.summary.self_sufficiency_pct | default(0) }}"
        unit_of_measurement: "%"
```

Add `powersoc_token: <your token>` to `secrets.yaml`.

The `/api/home/plan` endpoint exposes the battery charge recommendation, which
you can drive an automation from:

```yaml
rest:
  - resource: "http://192.168.1.50:8073/api/home/plan"
    headers:
      X-PowerSOC-Token: !secret powersoc_token
    scan_interval: 1800
    sensor:
      - name: "PowerSOC Charge Plan"
        value_template: "{{ value_json.data.recommended_action | default('unknown') }}"
        json_attributes_path: "$.data"
        json_attributes: ["headline", "target_soc_pct", "charge_kwh"]

automation:
  - alias: "Grid-charge battery when PowerSOC recommends it"
    trigger:
      - platform: state
        entity_id: sensor.powersoc_charge_plan
        to: "grid_charge"
    condition:
      - condition: time
        after: "22:00:00"
    action:
      - service: switch.turn_on
        target: {entity_id: switch.inverter_force_charge}
```

For programmatic consumers the token-gated `/api/home/*` JSON and the Prometheus
`/metrics` endpoint cover most needs without an extra MQTT broker; native MQTT
discovery can be layered on later if wanted.

## Prometheus + Grafana

`prometheus.yml`:

```yaml
scrape_configs:
  - job_name: powersoc
    scrape_interval: 60s
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials: <your PowerSOC home-API token>
    static_configs:
      - targets: ["192.168.1.50:8073"]
```

Exposed gauges include `powersoc_pv_watts`, `powersoc_battery_soc_percent`,
`powersoc_grid_watts`, `powersoc_house_load_watts`, `powersoc_today_*_kwh`,
`powersoc_today_net_cost_pence`, `powersoc_today_self_sufficiency_percent`,
`powersoc_import_rate_pence`, and `powersoc_carbon_intensity_grams`.

**Ready-made Grafana dashboard:** import `docs/grafana-dashboard.json`
(Dashboards → New → Import → Upload JSON), and pick your Prometheus datasource
when prompted. It has live power stats, a power-flow time series, today's
energy/cost, and a carbon-vs-rate panel.

**Alert rules:** load `docs/prometheus-powersoc-alerts.yml` via `rule_files:`
in `prometheus.yml` for scrape-down, battery-low, and high-cost alerts.
