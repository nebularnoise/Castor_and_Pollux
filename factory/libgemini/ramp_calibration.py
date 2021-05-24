# Copyright (c) 2021 Alethea Katherine Flowers.
# Published under the standard MIT License.
# Full text available at: https://opensource.org/licenses/MIT

import argparse
import json
import os.path
import pathlib
import statistics
import time

import pyvisa as visa
from wintertools import interactive, log, oscilloscope, tui

from libgemini import gemini, oscillators, reference_calibration

here = os.path.abspath(os.path.dirname(__file__))
period_to_dac_code = reference_calibration.castor.copy()
start_color = (1.0, 1.0, 0.0)
end_color = (0.5, 0.6, 1.0)

AVERAGE_COUNT = 2


# Use max because PK-PK has poor resolution and also includes negative transients.
def _measure_max(scope, scope_channel):
    return statistics.mean(
        scope.get_max(scope_channel) for _ in range(0, AVERAGE_COUNT)
    )


def _manual_seek(gem, dac_channel, charge_code):
    gem.set_dac(dac_channel, charge_code, 0)
    adjuster = interactive.adjust_value(charge_code, min=0, max=4095)
    output = tui.Updateable()

    with output:
        for value in adjuster:
            charge_code = value
            gem.set_dac(dac_channel, charge_code, 0)
            print(
                f"code: {charge_code}, voltage: {oscillators.charge_code_to_volts(charge_code):03f}"
            )
            output.update()

    return charge_code


def _set_scope_time_division(scope, frequency):
    if frequency > 1200:
        scope.set_time_division("100us")
    elif frequency > 700:
        scope.set_time_division("200us")
    elif frequency > 180:
        scope.set_time_division("500us")
    elif frequency > 90:
        scope.set_time_division("1ms")
    elif frequency > 46:
        scope.set_time_division("2ms")
    else:
        scope.set_time_division("5ms")


def _calibrate_oscillator(gem, scope, oscillator):
    bar = tui.Bar()

    if oscillator == 0:
        scope_channel = "c1"
        scope.enable_channel("c1")
        scope.disable_channel("c2")
        dac_channel = 0
    else:
        scope_channel = "c2"
        scope.enable_channel("c2")
        scope.disable_channel("c1")
        dac_channel = 2

    scope.set_trigger_level(scope_channel, "1.65V")
    scope.set_cursor_type("Y")
    scope.set_vertical_cursor(scope_channel, "-3.3V", "0V")
    scope.set_vertical_division(scope_channel, "800mV")
    scope.set_vertical_offset(scope_channel, "-1.65V")
    scope.show_measurement(scope_channel, "PKPK")
    scope.show_measurement(scope_channel, "MAX")

    scope.set_time_division("10ms")

    # Wait a moment for the scope to get ready.
    time.sleep(0.2)

    last_dac_code = 0

    for n, (period, dac_code) in enumerate(period_to_dac_code.items()):
        progress = n / (len(period_to_dac_code) - 1)

        if dac_code < last_dac_code:
            dac_code = last_dac_code

        # Adjust the oscilloscope's time division as needed.
        frequency = oscillators.timer_period_to_frequency(period)
        _set_scope_time_division(scope, frequency)

        bar.draw(
            tui.Segment(progress, color=tui.gradient(start_color, end_color, progress)),
        )

        log.info(f"Calibrating ramp for {frequency=:.2f} Hz {period=}")

        # If we've measured more than twice, we have enough info to determine
        # the slope of the charge voltage - it should be pretty much linear, so
        # we can guess a code very close to the right one.
        if n > 2:
            x_1, y_1 = list(period_to_dac_code.items())[n - 1]
            x_2, y_2 = list(period_to_dac_code.items())[0]
            x_1 = oscillators.timer_period_to_frequency(x_1)
            x_2 = oscillators.timer_period_to_frequency(x_2)
            slope = (y_2 - y_1) / (x_2 - x_1)
            y_intercept = y_2 - (slope * x_2)
            dac_code = min(4095, round(y_intercept + (slope * frequency)))
            log.info(f"Guessed DAC code as {dac_code} from slope {slope:02f}")

        gem.set_period(oscillator, period)

        calibrated_code = _manual_seek(gem, dac_channel, dac_code)

        period_to_dac_code[period] = calibrated_code

        magnitude = _measure_max(scope, scope_channel)

        log.success(
            f"Calibrated to {calibrated_code} ({oscillators.charge_code_to_volts(calibrated_code):.03f} volts), magnitude: {magnitude:.2f} volts"
        )

        last_dac_code = calibrated_code

    return period_to_dac_code.copy()


def run(save):
    # Gemini setup
    log.info("Connecting to Gemini...")
    gem = gemini.Gemini()
    gem.enter_calibration_mode()

    initial_period, initial_dac_code = next(iter(period_to_dac_code.items()))
    time.sleep(0.1)
    gem.set_period(0, initial_period)
    gem.set_dac(0, initial_dac_code, 0)
    time.sleep(0.1)
    gem.set_period(1, initial_period)
    gem.set_dac(2, initial_dac_code, 0)

    # Oscilloscope setup.
    log.info("Configuring oscilloscope...")
    resource_manager = visa.ResourceManager("@ivi")
    scope = oscilloscope.Oscilloscope(resource_manager)

    # scope.reset()
    scope.enable_bandwidth_limit()
    scope.set_intensity(50, 100)

    # Enable both channels initially so that it's clear if the programming
    # board isn't connecting to the POGO pins.
    scope.set_time_division("10ms")
    scope.enable_channel("c1")
    scope.enable_channel("c2")
    scope.set_vertical_division("c1", "800mV")
    scope.set_vertical_division("c2", "800mV")
    scope.set_vertical_offset("c1", "-1.65V")
    scope.set_vertical_offset("c2", "-1.65V")

    log.warning("Connect PROBE ONE to RAMP A")
    log.warning("Connect PROBE TWO to RAMP B")
    log.warning("Confirm sawtooth waveforms are visible before continuing!")
    interactive.continue_when_ready()

    # Calibrate both oscillators
    log.section("Calibrating Castor...", depth=2)
    castor_calibration = _calibrate_oscillator(gem, scope, 0)

    lowest_voltage = oscillators.charge_code_to_volts(min(castor_calibration.values()))
    highest_voltage = oscillators.charge_code_to_volts(max(castor_calibration.values()))
    log.success(
        f"\nCalibrated:\n- Lowest: {lowest_voltage:.2f}v\n- Highest: {highest_voltage:.2f}v\n"
    )

    log.section("Calibrating Pollux...", depth=2)
    pollux_calibration = _calibrate_oscillator(gem, scope, 1)

    lowest_voltage = oscillators.charge_code_to_volts(min(pollux_calibration.values()))
    highest_voltage = oscillators.charge_code_to_volts(max(pollux_calibration.values()))
    log.success(
        f"\nCalibrated:\n- Lowest: {lowest_voltage:.2f}v\n- Highest: {highest_voltage:.2f}v\n"
    )

    log.section("Saving calibration table...", depth=2)

    local_copy = pathlib.Path("calibrations") / f"{gem.serial_number}.ramp.json"
    local_copy.parent.mkdir(parents=True, exist_ok=True)

    with local_copy.open("w") as fh:
        data = {
            "castor": castor_calibration,
            "pollux": pollux_calibration,
        }
        json.dump(data, fh)

    log.success(f"Saved local copy to {local_copy}")

    if save:
        output = tui.Updateable()
        bar = tui.Bar()

        log.info("Sending LUT values to device...")

        with output:
            for n, timer_period in enumerate(castor_calibration.keys()):
                progress = n / (len(castor_calibration.values()) - 1)
                bar.draw(
                    tui.Segment(
                        progress,
                        color=tui.gradient(start_color, end_color, progress),
                    ),
                )
                output.update()

                castor_code = castor_calibration[timer_period]
                pollux_code = pollux_calibration[timer_period]

                gem.write_lut_entry(n, timer_period, castor_code, pollux_code)

                log.debug(
                    f"Set LUT entry {n} to {timer_period=}, {castor_code=}, {pollux_code=}."
                )

        log.info("Committing LUT to NVM...")
        gem.write_lut()

        checksum = 0
        for dac_code in castor_calibration.values():
            checksum ^= dac_code

        log.success(f"Calibration table written, checksum: {checksum:04x}")

    else:
        log.warning("Dry run enabled, calibration table not saved to device.")

    gem.close()

    print("")
    log.success("Done!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        default=False,
        help="Don't save the calibration values.",
    )

    args = parser.parse_args()

    run(not args.dry_run)
