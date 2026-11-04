# Contributing

Bug reports, model reports and pull requests are welcome. This integration writes to a real inverter, so read
[Writing to the inverter](README.md#writing-to-the-inverter) before you test anything that changes a register.

## Ground rules

- **Never publish a serial number or a Bluetooth address.** Not in an issue, a test, a commit or a log. The
  diagnostics download already leaves both out.
- **Do not test writes on an inverter you do not own.** With *Allow writes* on, the integration can change grid
  protection and battery limits with no safety checks. Note the old value first, and check the new one on the
  inverter's display.
- Report security issues privately, see [SECURITY.md](SECURITY.md).

## Development setup

Python 3.14, the version CI uses.

```sh
git clone https://github.com/csorbalazs306/ha-deye-bluetooth.git
cd ha-deye-bluetooth
python3.14 -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements_test.txt ruff
```

Home Assistant itself is not needed: `pytest-homeassistant-custom-component` brings the test harness. Bluetooth is
not needed either: `tests/test_integration.py` replaces the link with a fake one.

## Checks

Run both before you push. CI runs them on every push and pull request, together with hassfest and the HACS
validation.

```sh
ruff check .
pytest
```

- `tests/test_pure.py` loads `protocol.py` and `registers.py` directly, without Home Assistant. Add a test there for
  every framing or decoding change.
- `tests/test_integration.py` sets the integration up in a real Home Assistant with a fake link. Add a test there for
  every entity, option or service change, and assert the value or the write, not only that the entity exists.

A bug fix needs a test that fails before it and passes after. A new feature needs a test for the normal case and one
for the refusal or the missing data.

## Layout

| File | What belongs there |
|---|---|
| `__init__.py` | the coordinator, the poll groups and the two service actions |
| `registers.py` | the register map: poll blocks, values, decoding, setting ranges |
| `protocol.py` | Modbus RTU framing and the logger's AT console; no I/O, no Home Assistant |
| `transport.py` | the GATT link: connect, notify, command, read, write |
| `options.py` | every option: section, default, range |
| `config_flow.py` | discovery, the options page and its validation |
| `entity.py` | the entity base, the summary sensors and the shared unit classes |
| `sensor.py`, `binary_sensor.py`, `switch.py`, `number.py`, `select.py`, `time.py` | the platforms |
| `diagnostics.py` | the diagnostics download |
| `docs/protocol.md` | the measured logger protocol |

Two rules keep the code testable and the map in one place:

- `protocol.py` and `registers.py` must not import Home Assistant. `tests/test_pure.py` relies on it.
- A register number, scale or option text belongs in `registers.py`, not in a platform file. Platforms build their
  entities from the maps (`r.VALUES`, `r.NUMBERS`, `r.SELECTS`, `r.TIMES`, `r.ON_OFF_SWITCHES`).

## Adding a value

1. Add a `Value` to `VALUES` in `registers.py`: `key`, `name`, address, and the unit, scale, `signed`, `words`,
   `offset`, `options`, `mask` or `fmt` that apply.
2. Put its addresses in a block of `LIVE`, `FAST` or `SLOW`. `test_blocks_fit_the_logger` checks that every value is
   polled, that no address is read twice and that no block is longer than the logger answers.
3. Add a decoding case to `tests/test_pure.py` with the words the inverter would send.
4. Only if the register is a single-word setting, add it to `NUMBERS`, `SELECTS`, `TIMES` or `ON_OFF_SWITCHES`. The
   ranges only fit the register; they are not safety limits.

Readings, identity, the inverter clock and the Modbus address are not controls.

## Adding an option

Add an `Option(key, section, default, low, high)` to `options.py`; `DEFAULTS` is generated from the tuple. Add its
label and description to `translations/en.json` under `options.step.init.sections.<section>.data` and
`data_description`. If the option interacts with another, as the stale limits do with the poll intervals, check it in
`_errors` in `config_flow.py` and add the error string there.

The coordinator reads an option through `self.option(key)`, so the default and the configured value come from the
same place.

## Testing on hardware

- Close the Deye phone app: the logger takes one Bluetooth connection at a time.
- Leave *Allow writes* off until every reading looks right. Use the `deye_bluetooth.read_registers` action to probe
  addresses.
- Watch the debug log (`custom_components.deye_bluetooth: debug`) while you test; a dropped link is usually signal.
- For a model report, attach the diagnostics file and say which values look wrong, what the inverter's display shows
  and which firmware the logger runs.

## Commits and pull requests

- Branch from `main`, one logical change per pull request.
- Write the commit message in the imperative: what changed and why. Reference the issue when there is one.
- Keep unrelated formatting out of the diff.
- A user-visible change needs a `CHANGELOG.md` entry. When you bump the version, change it in both `pyproject.toml`
  and `custom_components/deye_bluetooth/manifest.json`.
- Say in the pull request what you tested: the commands you ran and, for hardware changes, the model you ran them on.

## Style

- Ruff with `E`, `F`, `I`, `B` and `UP`, line length 130.
- Type hints on new functions; comments say why, not what.
- The docstrings carry the protocol and inverter knowledge. Keep them accurate when you change behaviour.
- Home Assistant conventions: no blocking I/O in the event loop, poll failures at debug level, entity ids and unique
  ids through `DeyeEntity`.

## Reporting a bug

Include the Home Assistant version, the integration version, the inverter model and logger firmware, the steps to
reproduce, the expected and the actual behaviour, and the debug log plus the diagnostics file. Remove the serial
number and the Bluetooth address if you paste anything by hand.

## License

By contributing you agree that your work is released under the [MIT license](LICENSE).
