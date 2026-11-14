"""The parts free of Home Assistant: the BLE console framing and the register decoding."""
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "deye_bluetooth"


def load(name):
    spec = importlib.util.spec_from_file_location(f"deye_bluetooth_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


p = load("protocol")
r = load("registers")


def _reply(body):
    return "+ok=" + p._frame(body).hex()


class ProtocolTest(unittest.TestCase):
    def test_read_frame_as_captured(self):
        # AT+INVDATA=8,01030002000125CA -> +ok=0103020104B817 (captured from a real logger)
        frame = p.read_frame(2, 1)
        self.assertEqual(p.at_command(frame), b"AT+INVDATA=8,01030002000125CA\n")
        self.assertEqual(p.parse_read("+ok=0103020104B817", 1), [0x0104])

    def test_write_frame_and_ack(self):
        frame = p.write_frame(0x008F, 100)
        self.assertEqual(len(frame), 11)
        self.assertTrue(p.at_command(frame).startswith(b"AT+INVDATA=11,0110008F00010200"))
        ack = bytes((1, 0x10, 0x00, 0x8F, 0x00, 0x01))
        p.check_write_ack(_reply(ack), frame)
        other = bytes((1, 0x10, 0x00, 0x90, 0x00, 0x01))
        with self.assertRaises(p.LinkError):
            p.check_write_ack(_reply(other), frame)

    def test_rejects_bad_replies(self):
        for reply in ("ERROR", "+ok=0103020104B818", "+ok=zz", "+ok=0183020000"):
            with self.assertRaises(p.LinkError, msg=reply):
                p.parse_read(reply, 1)
        exc = bytes((1, 0x83, 0x02))  # a refusal, not a link error: the link stays up
        with self.assertRaisesRegex(p.RegisterRejected, "exception 0x02"):
            p.parse_read(_reply(exc), 1)
        self.assertFalse(issubclass(p.RegisterRejected, p.LinkError))

    def test_handshake_and_limits(self):
        self.assertTrue(p.is_handshake("+ok=21521,21521\r\n"))
        self.assertFalse(p.is_handshake("+ok=0103"))
        with self.assertRaises(ValueError):
            p.read_frame(0, 17)


class RegistersTest(unittest.TestCase):
    def test_blocks_fit_the_logger(self):
        blocks = r.LIVE + r.FAST + r.SLOW
        for start, count in blocks:
            self.assertLessEqual(count, p.MAX_READ, start)
        polled = [start + i for start, count in blocks for i in range(count)]
        self.assertEqual(len(polled), len(set(polled)))  # no register read twice
        polled = set(polled)
        for v in r.VALUES:
            for word in range(v.words):
                self.assertIn(v.reg + word, polled, v.key)
        self.assertEqual(len(r.BY_KEY), len(r.VALUES))  # unique keys

    def test_the_live_blocks_hold_what_the_amp_guard_needs(self):
        live = {start + i for start, count in r.LIVE for i in range(count)}
        for key in ("grid_power", "load_power", *(f"grid_{q}_l{n}" for q in ("current", "power") for n in (1, 2, 3))):
            self.assertIn(r.BY_KEY[key].reg, live, key)

    def test_decoding(self):
        regs = {
            607: 32, 604: 0x10000 - 62, 610: 0x10000 - 25,  # grid +32 W, L1 -62 W, -0.25 A
            598: 2425, 609: 4995,  # 242.5 V, 49.95 Hz
            586: 1171, 541: 1374,  # 17.1 °C, 37.4 °C
            587: 5008, 588: 10, 590: 0x10000 - 17,  # 50.08 V, 10 %, -17 W
            522: 402, 523: 0, 534: 0x86A0, 535: 0x0001,  # 40.2 kWh; 32 bit: 10000.0 kWh
            672: 300, 673: 0, 674: 0, 675: 5,
        }
        d = {v.key: r.decode_value(v, regs) for v in r.VALUES}
        self.assertEqual((d["grid_power"], d["grid_power_l1"], d["grid_current_l1"]), (32, -62, -0.25))
        self.assertEqual((d["grid_voltage_l1"], d["grid_frequency"]), (242.5, 49.95))
        self.assertEqual((d["battery_temperature"], d["ac_temperature"]), (17.1, 37.4))
        self.assertEqual((d["battery_voltage"], d["battery_soc"], d["battery_power"]), (50.08, 10, -17))
        self.assertEqual((d["total_import"], d["total_pv"]), (40.2, 10000.0))
        self.assertIsNone(d["load_power"])  # not polled yet
        self.assertEqual(r.pv_power(regs), 305)

    def test_times_texts_and_bits(self):
        regs = {148: 2230, 10: 0xC05C, 223: 0, 22: 0x0203, 552: 0x0C, 111: 9, 62: 0x1A0B, 63: 0x020C, 64: 0x1E05}
        def get(key):
            return r.decode_value(r.BY_KEY[key], regs)
        self.assertEqual(get("program_1_time"), "22:30")
        self.assertEqual(get("mcu_board_version"), "C05C")
        self.assertEqual(get("bms_type"), "PYLON")
        self.assertEqual((get("mppts"), get("phases")), (2, 3))
        self.assertEqual(get("relay_generator"), 1)
        self.assertEqual(get("battery_operation_mode"), "unknown (9)")
        self.assertEqual(get("inverter_clock"), "2026-11-02 12:30:05")
        self.assertIsNone(r.decode_value(r.BY_KEY["inverter_clock"], {62: 1}))
        self.assertIsNone(r.decode_value(r.BY_KEY["program_2_time"], regs))

    def test_battery_current_matches_the_power(self):
        # measured 2026-11-02: -766 W at 52.25 V, register 591 read 0.08 A, 594 read -14.75 A
        regs = {591: 8, 594: 0x10000 - 1475}
        self.assertEqual(r.decode_value(r.BY_KEY["battery_current"], regs), -14.75)

    def test_encoding_is_the_opposite_of_decoding(self):
        for key, value, raw in (("program_1_soc", 80, 80), ("max_sell_power", 5000, 5000),
                                ("battery_float_voltage", 54.5, 5450), ("battery_temperature", 17.1, 1171)):
            v = r.BY_KEY[key]
            self.assertEqual(r.encode_value(v, value), raw, key)
            self.assertEqual(r.decode_value(v, {v.reg: raw}), value, key)
        with self.assertRaises(ValueError):
            r.encode_value(r.BY_KEY["program_1_soc"], -1)
        compensation = r.BY_KEY["battery_temperature_compensation"]
        self.assertEqual(r.encode_value(compensation, -30), 0x10000 - 30)
        self.assertEqual(r.decode_value(compensation, {compensation.reg: 0x10000 - 30}), -30)

    def test_the_controls_are_single_known_registers(self):
        keys = [*r.NUMBERS, *r.SELECTS, *r.TIMES, *r.ON_OFF_SWITCHES]
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertEqual(r.BY_KEY[key].words, 1, key)
        for key in ("grid_protection_voltage_high", "grid_protection_frequency_low", "battery_float_voltage",
                    "battery_control_mode", "bms_type", "grid_voltage_setting", "ac_coupling"):
            self.assertIn(key, keys)
        for key in ("modbus_address", "bms_soc", "battery_voltage", "fault_1"):  # identity and readings
            self.assertNotIn(key, keys)

    def test_the_live_blocks_hold_the_power_flow(self):
        live = {start + i for start, count in r.LIVE for i in range(count)}
        for key in ("battery_soc", "battery_power", *(f"pv{n}_power" for n in (1, 2, 3, 4))):
            self.assertIn(r.BY_KEY[key].reg, live, key)
        self.assertEqual(r.LIVE_REGS, frozenset(live))

    def test_the_live_and_fast_blocks_as_measured(self):
        # grid voltages are not live: nothing needs them every second, and they read with the 15 s poll
        self.assertEqual(r.LIVE, [(588, 3), (604, 9), (650, 6), (672, 4)])
        self.assertIn((598, 3), r.FAST)
        self.assertNotIn((654, 2), r.FAST)
        polled = {start + i for start, count in r.LIVE + r.FAST + r.SLOW for i in range(count)}
        for reg in (598, 599, 600):
            self.assertIn(reg, polled)
            self.assertNotIn(reg, {start + i for start, count in r.LIVE for i in range(count)})
        for reg in (601, 602, 603):  # never read on the hardware, and no value uses them
            self.assertNotIn(reg, polled)
            self.assertFalse(any(v.reg == reg or reg in range(v.reg, v.reg + v.words) for v in r.VALUES), reg)
        self.assertEqual(r.BY_KEY["load_frequency"].reg, 655)
        self.assertIn(655, {start + i for start, count in r.LIVE for i in range(count)})

    def test_slow_regs_are_the_settings_blocks(self):
        self.assertEqual(len(r.SLOW_REGS), sum(count for _, count in r.SLOW))
        self.assertIn(117, r.SLOW_REGS)  # battery low SoC
        self.assertIn(3, r.SLOW_REGS)  # serial
        self.assertNotIn(586, r.SLOW_REGS)  # battery temperature: FAST
        self.assertNotIn(604, r.SLOW_REGS)  # grid power: LIVE

    def test_hhmm_decodes_without_validating_and_encodes_back(self):
        self.assertEqual(r.decode_hhmm(2230), (22, 30))
        self.assertEqual(r.decode_hhmm(5), (0, 5))
        self.assertEqual(r.decode_hhmm(2460), (24, 60))  # not validated here: the time entity refuses it
        self.assertIsNone(r.decode_hhmm(None))
        self.assertEqual(r.encode_hhmm(22, 30), 2230)
        self.assertEqual(r.encode_hhmm(0, 5), 5)
        for raw in (0, 1, 959, 2359):
            self.assertEqual(r.encode_hhmm(*r.decode_hhmm(raw)), raw)
        self.assertEqual(r.decode_value(r.BY_KEY["program_2_time"], {149: 930}), "09:30")

    def test_work_mode_name_is_the_work_mode_register_as_text(self):
        self.assertEqual(r.decode_value(r.BY_KEY["work_mode_name"], {142: 1}), "zero export to load")
        self.assertEqual(r.decode_value(r.BY_KEY["work_mode_name"], {142: 7}), "unknown (7)")
        self.assertIsNone(r.decode_value(r.BY_KEY["work_mode_name"], {}))  # missing: unavailable
        self.assertEqual(r.BY_KEY["work_mode_name"].reg, r.BY_KEY["work_mode"].reg)

    def test_serial(self):
        words = [int.from_bytes(b"1234567890"[i:i + 2], "big") for i in range(0, 10, 2)]
        self.assertEqual(r.decode_serial({3 + i: w for i, w in enumerate(words)}), "1234567890")
        self.assertIsNone(r.decode_serial({3: 1}))

    def test_identity_as_read_from_the_inverter(self):
        # registers 0-21 of a SUN-15K-SG05LP3 (the serial replaced)
        words = [5, 1, 260, 12594, 13108, 13622, 14136, 14640, 0, 0, 0, 6153, 53770, 0,
                 8455, 4469, 0, 4097, 49244, 0, 18928, 2]
        regs = dict(enumerate(words))
        self.assertEqual(r.decode_value(r.BY_KEY["rated_power"], regs), 15000.0)
        self.assertEqual(r.decode_firmware(regs, "main"), "2107-1175-1809")
        self.assertEqual(r.decode_firmware(regs, "hmi"), "1001-C05C")
        self.assertEqual(r.decode_firmware(regs, "protocol"), "0104")
        self.assertEqual(r.decode_serial(regs), "1234567890")
        self.assertEqual(r.rated_power(regs), 15000.0)
        self.assertEqual(r.device_model(regs), "LV 3-phase hybrid, 15 kW")
        self.assertIn(regs[r.DEVICE_TYPE_REG], r.VERIFIED_DEVICE_TYPES)


    # --- behavioural coverage added by the review loops ----------------------
    def test_crc16_known_vectors(self):
        self.assertEqual(p.crc16(b"123456789"), bytes.fromhex("374B"))
        self.assertEqual(p.crc16(b""), bytes.fromhex("FFFF"))
        self.assertEqual(p.crc16(bytes.fromhex("010300020001")), bytes.fromhex("25CA"))
        self.assertEqual(p.crc16(b"123456789")[::-1], (0x4B37).to_bytes(2, "big"))

    def test_parse_read_multiple_registers(self):
        body = bytes((1, p.READ, 4)) + (0x1234).to_bytes(2, "big") + (0xABCD).to_bytes(2, "big")
        self.assertEqual(p.parse_read(_reply(body), 2), [0x1234, 0xABCD])
        words = list(range(16))
        body = bytes((1, p.READ, 32)) + b"".join(w.to_bytes(2, "big") for w in words)
        self.assertEqual(p.parse_read(_reply(body), 16), words)

    def test_reply_error_validation(self):
        bad_bodies = (
            bytes((1, 0x10, 2, 0, 1)),          # wrong function
            bytes((1, p.READ, 4, 0, 1, 0, 2)),  # wrong byte count
            bytes((1, p.READ, 2, 0, 1, 0, 2)),  # wrong length
        )
        for body in bad_bodies:
            reply = _reply(body)
            with self.assertRaisesRegex(p.LinkError, "not a read of"):
                p.parse_read(reply, 1)
        with self.assertRaises(p.LinkError):
            p.parse_read("+ok=0103", 1)
        ack = bytes((1, p.READ, 0, 1, 0, 1))
        with self.assertRaises(p.LinkError):
            p.check_write_ack(_reply(ack), p.write_frame(1, 1))
        exc = bytes((1, 0x83, 0x02))
        with self.assertRaisesRegex(p.RegisterRejected, "function 0x03"):
            p.parse_read(_reply(exc), 1)
        self.assertFalse(p.is_handshake("+ok=21521,21521,9"))

    def test_frame_validation_and_overrides(self):
        with self.assertRaises(ValueError):
            p.read_frame(0, 0)
        with self.assertRaises(ValueError):
            p.write_frame(0, -1)
        with self.assertRaises(ValueError):
            p.write_frame(0, 0x10000)
        self.assertEqual(p.write_frame(0, 0xFFFF)[-4:-2], b"\xFF\xFF")
        self.assertEqual(p.read_frame(2, 1, slave=5)[0], 5)
        p.read_frame(0, 17, max_read=32)
        with self.assertRaises(ValueError):
            p.read_frame(0, 9, max_read=8)

    def test_two_word_value_needs_both_words(self):
        self.assertIsNone(r.decode_value(r.BY_KEY["total_import"], {522: 402}))
        self.assertIsNone(r.decode_value(r.BY_KEY["rated_power"], {20: 50000}))
        self.assertEqual(r.decode_value(r.BY_KEY["total_import"], {522: 402, 523: 0}), 40.2)
        alarm = r.decode_value(r.BY_KEY["device_alarm"], {553: 0x86A0, 554: 1})
        self.assertEqual(alarm, 100000)
        self.assertIsInstance(alarm, int)
        self.assertEqual(r.decode_value(r.BY_KEY["fault_1"], {555: 0xFFFF, 556: 0xFFFF}), 0xFFFFFFFF)

    def test_encoding_enforces_the_register_word_range(self):
        soc = r.BY_KEY["program_1_soc"]
        comp = r.BY_KEY["battery_temperature_compensation"]
        with self.assertRaises(ValueError):
            r.encode_value(soc, 65536)
        self.assertEqual(r.encode_value(soc, 65535), 0xFFFF)
        for bad in (-32769, 32768):
            with self.assertRaises(ValueError):
                r.encode_value(comp, bad)
        self.assertEqual(r.encode_value(comp, -32768), 0x8000)
        self.assertEqual(r.encode_value(comp, 32767), 0x7FFF)
        self.assertEqual(r.decode_value(comp, {comp.reg: 0x8000}), -32768)

    def test_unusual_scales_and_text_formats(self):
        self.assertEqual(r.decode_value(r.BY_KEY["grid_max_export_power"], {231: 1000}), 10000.0)
        self.assertEqual(r.encode_value(r.BY_KEY["grid_max_export_power"], 10000), 1000)
        self.assertEqual(r.decode_value(r.BY_KEY["battery_equalization_time"], {106: 3}), 1.5)
        self.assertEqual(r.encode_value(r.BY_KEY["battery_equalization_time"], 1.5), 3)
        self.assertEqual(r.decode_value(r.BY_KEY["program_1_time"], {148: 5}), "00:05")
        self.assertEqual(r.decode_value(r.BY_KEY["program_1_time"], {148: 930}), "09:30")
        self.assertEqual(r.decode_value(r.BY_KEY["afci_version"], {12: 0x000C}), "000C")
        self.assertEqual(r.decode_value(r.BY_KEY["mcu_version"], {13: 0xFFFF}), "FFFF")
        self.assertIsNone(r.decode_value(r.BY_KEY["afci_version"], {}))

    def test_number_limits_are_numbers_not_option_names(self):
        self.assertIsNotNone(r.RATED)  # opaque sentinel: None is never a valid limit
        for key, (low, high, step) in r.NUMBERS.items():
            self.assertIsInstance(low, (int, float), key)
            self.assertIsInstance(step, (int, float), key)
            self.assertTrue(high is r.RATED or isinstance(high, (int, float)), key)
            self.assertNotIsInstance(low, str, key)
            self.assertIsNot(low, r.RATED, key)
            self.assertIsNot(step, r.RATED, key)
            self.assertGreater(step, 0, key)
            if high is r.RATED:
                self.assertGreaterEqual(low, 0, key)
            else:
                self.assertLess(low, high, key)

    def test_fixed_ranges_and_grid_threshold(self):
        self.assertEqual(r.GRID_PRESENT_MIN_V, 100)
        self.assertEqual(r.GRID_RELAY_BIT, 0x04)
        self.assertEqual(r.NUMBERS["battery_low_soc"], (0, 100, 1))
        self.assertEqual(r.NUMBERS["zero_export_power"], (0, 10000, 1))
        self.assertEqual(r.NUMBERS["battery_max_charge_current"], (0, 2000, 1))
        self.assertEqual(r.NUMBERS["max_sell_power"], (0, r.RATED, 100))
        self.assertEqual(r.DEFAULT_RATED_W, 20000)

    def test_select_options_are_non_empty_int_to_str_maps(self):
        for key, opts in r.SELECTS.items():
            self.assertIsInstance(opts, dict, key)
            self.assertGreater(len(opts), 0, key)
            for raw, label in opts.items():
                self.assertIsInstance(raw, int, key)
                self.assertIsInstance(label, str, key)
            self.assertEqual(len(set(opts.values())), len(opts), key)

    def test_all_control_keys_resolve_to_single_register_values(self):
        keys = [*r.NUMBERS, *r.SELECTS, *r.TIMES, *r.ON_OFF_SWITCHES]
        self.assertEqual(set(keys) - set(r.BY_KEY), set())
        for key in r.TIMES:
            self.assertEqual(r.BY_KEY[key].fmt, "time", key)
        self.assertEqual(r.TIMES, [f"program_{i}_time" for i in range(1, 7)])
        self.assertEqual(r.ON_OFF_SWITCHES, ["solar_sell"])


if __name__ == "__main__":
    unittest.main()
