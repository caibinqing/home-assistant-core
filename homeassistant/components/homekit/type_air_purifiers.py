"""Class to hold all air purifier accessories."""

import logging
from typing import Any

from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_AIR_PURIFIER
from pyhap.util import callback as pyhap_callback

from homeassistant.components.fan import (
    ATTR_OSCILLATING,
    ATTR_PERCENTAGE,
    ATTR_PERCENTAGE_STEP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    DOMAIN as FAN_DOMAIN,
    SERVICE_OSCILLATE,
    SERVICE_SET_PERCENTAGE,
    SERVICE_SET_PRESET_MODE,
    FanEntityFeature,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import (
    Event,
    HassJobType,
    EventStateChangedData,
    State,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event

from .accessories import TYPES, HomeAccessory
from .const import (
    CHAR_ACTIVE,
    CHAR_AIR_QUALITY,
    CHAR_CURRENT_AIR_PURIFIER_STATE,
    CHAR_CURRENT_HUMIDITY,
    CHAR_CURRENT_TEMPERATURE,
    CHAR_LOCK_PHYSICAL_CONTROLS,
    CHAR_NAME,
    CHAR_ON,
    CHAR_PM10_DENSITY,
    CHAR_PM25_DENSITY,
    CHAR_ROTATION_SPEED,
    CHAR_SWING_MODE,
    CHAR_TARGET_AIR_PURIFIER_STATE,
    CONF_LINKED_HUMIDITY_SENSOR,
    CONF_LINKED_LOCK_PHYSICAL_CONTROLS_SWITCH,
    CONF_LINKED_PM10_SENSOR,
    CONF_LINKED_PM25_SENSOR,
    CONF_LINKED_TEMPERATURE_SENSOR,
    CONF_PRESET_MODE_AUTO,
    CONF_PRESET_MODE_MANUAL,
    PROP_MIN_STEP,
    SERV_AIR_PURIFIER,
    SERV_AIR_QUALITY_SENSOR,
    SERV_HUMIDITY_SENSOR,
    SERV_SWITCH,
    SERV_TEMPERATURE_SENSOR,
)
from .util import (
    cleanup_name_for_homekit,
    density_to_air_quality,
    density_to_air_quality_pm10,
)

_LOGGER = logging.getLogger(__name__)

CURRENT_STATE_INACTIVE = 0
CURRENT_STATE_IDLE = 1
CURRENT_STATE_ACTIVE = 2
TARGET_STATE_MANUAL = 0
TARGET_STATE_AUTOMATIC = 1


@TYPES.register("AirPurifier")
class AirPurifier(HomeAccessory):
    """Generate an AirPurifier accessory for a fan entity.

    Currently supports, in addition to Fan properties:
    temperature, humidity, PM2.5, auto mode.
    """

    def __init__(self, *args: Any) -> None:
        """Initialize a new AirPurifier accessory object."""
        super().__init__(*args, category=CATEGORY_AIR_PURIFIER)
        self.chars: list[str] = []
        state = self.hass.states.get(self.entity_id)
        assert state
        self._reload_on_change_attrs.extend(
            (
                ATTR_PERCENTAGE_STEP,
                ATTR_PRESET_MODES,
            )
        )

        features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        percentage_step = state.attributes.get(ATTR_PERCENTAGE_STEP, 1)
        self.preset_modes: list[str] | None = state.attributes.get(
            ATTR_PRESET_MODES, []
        )
        preset_mode_manual = self.config.get(CONF_PRESET_MODE_MANUAL, "manual")
        preset_mode_auto = self.config.get(CONF_PRESET_MODE_AUTO, "auto")
        self.preset_manual: str | None = next(
            filter(lambda x: x.lower() == preset_mode_manual, self.preset_modes), None
        )
        self.preset_auto: str | None = next(
            filter(lambda x: x.lower() == preset_mode_auto, self.preset_modes), None
        )
        self.preset_modes = [
            x
            for x in self.preset_modes
            if x not in [self.preset_manual, self.preset_auto]
        ]
        # _LOGGER.debug("%s: preset_modes: %s", self.entity_id, self.preset_modes)
        # _LOGGER.debug("%s: preset_auto: %s", self.entity_id, self.preset_auto)

        if features & FanEntityFeature.OSCILLATE:
            self.chars.append(CHAR_SWING_MODE)
        if features & FanEntityFeature.SET_SPEED:
            self.chars.append(CHAR_ROTATION_SPEED)

        self.linked_lock_physical_controls_switch = self.config.get(
            CONF_LINKED_LOCK_PHYSICAL_CONTROLS_SWITCH
        )
        if self.linked_lock_physical_controls_switch:
            self.chars.append(CHAR_LOCK_PHYSICAL_CONTROLS)

        serv_air_purifier = self.add_preload_service(SERV_AIR_PURIFIER, self.chars)
        self.set_primary_service(serv_air_purifier)
        self.char_active = serv_air_purifier.configure_char(CHAR_ACTIVE, value=0)

        self.char_current_air_purifier_state: Characteristic = (
            serv_air_purifier.configure_char(
                CHAR_CURRENT_AIR_PURIFIER_STATE,
                value=CURRENT_STATE_INACTIVE,
            )
        )

        self.char_target_air_purifier_state: Characteristic = (
            serv_air_purifier.configure_char(
                CHAR_TARGET_AIR_PURIFIER_STATE,
                value=TARGET_STATE_MANUAL,
            )
        )

        self.preset_mode_chars: dict[str, Characteristic] = {}

        if self.preset_modes:
            for preset_mode in self.preset_modes:
                preset_serv = self.add_preload_service(
                    SERV_SWITCH, CHAR_NAME, unique_id=preset_mode
                )
                serv_air_purifier.add_linked_service(preset_serv)
                preset_serv.configure_char(
                    CHAR_NAME,
                    value=cleanup_name_for_homekit(
                        f"{self.display_name} {preset_mode}"
                    ),
                )

                def setter_callback(value: int, preset_mode: str = preset_mode) -> None:
                    return self.set_preset_mode(value, preset_mode)

                self.preset_mode_chars[preset_mode] = preset_serv.configure_char(
                    CHAR_ON,
                    value=False,
                    setter_callback=setter_callback,
                )

        self.char_current_temperature: Characteristic | None = None
        self.char_current_humidity: Characteristic | None = None

        self.linked_temperature_sensor = self.config.get(CONF_LINKED_TEMPERATURE_SENSOR)
        if self.linked_temperature_sensor:
            temperature_serv = self.add_preload_service(
                SERV_TEMPERATURE_SENSOR, [CHAR_NAME, CHAR_CURRENT_TEMPERATURE]
            )
            serv_air_purifier.add_linked_service(temperature_serv)
            temperature_serv.configure_char(
                CHAR_NAME,
                value=cleanup_name_for_homekit(f"{self.display_name} Temperature"),
            )
            self.char_current_temperature = temperature_serv.configure_char(
                CHAR_CURRENT_TEMPERATURE, value=21.0
            )

            temperature_state = self.hass.states.get(self.linked_temperature_sensor)
            if temperature_state:
                self._async_update_current_temperature(temperature_state)

        self.linked_humidity_sensor = self.config.get(CONF_LINKED_HUMIDITY_SENSOR)
        if self.linked_humidity_sensor:
            humidity_serv = self.add_preload_service(SERV_HUMIDITY_SENSOR, CHAR_NAME)
            serv_air_purifier.add_linked_service(humidity_serv)
            humidity_serv.configure_char(
                CHAR_NAME,
                value=cleanup_name_for_homekit(f"{self.display_name} Humidity"),
            )
            self.char_current_humidity = humidity_serv.configure_char(
                CHAR_CURRENT_HUMIDITY, value=50
            )

            humidity_state = self.hass.states.get(self.linked_humidity_sensor)
            if humidity_state:
                self._async_update_current_humidity(humidity_state)

        self.char_pm25_density: Characteristic | None = None
        self.char_pm10_density: Characteristic | None = None

        self.linked_pm25_sensor = self.config.get(CONF_LINKED_PM25_SENSOR)
        self.linked_pm10_sensor = self.config.get(CONF_LINKED_PM10_SENSOR)
        if self.linked_pm25_sensor or self.linked_pm10_sensor:
            chars = [CHAR_AIR_QUALITY, CHAR_NAME]
            if self.linked_pm25_sensor:
                chars.append(CHAR_PM25_DENSITY)
            if self.linked_pm10_sensor:
                chars.append(CHAR_PM10_DENSITY)
            air_quality_serv = self.add_preload_service(SERV_AIR_QUALITY_SENSOR, chars)
            serv_air_purifier.add_linked_service(air_quality_serv)
            air_quality_serv.configure_char(
                CHAR_NAME,
                value=cleanup_name_for_homekit(f"{self.display_name} Air Quality"),
            )
            self.char_air_quality = air_quality_serv.configure_char(CHAR_AIR_QUALITY)
            if self.linked_pm25_sensor:
                self.char_pm25_density = air_quality_serv.configure_char(
                    CHAR_PM25_DENSITY, value=0
                )
                pm25_state = self.hass.states.get(self.linked_pm25_sensor)
                if pm25_state:
                    self._async_update_current_pm25(pm25_state)
            if self.linked_pm10_sensor:
                self.char_pm10_density = air_quality_serv.configure_char(
                    CHAR_PM10_DENSITY, value=0
                )
                pm10_state = self.hass.states.get(self.linked_pm10_sensor)
                if pm10_state:
                    self._async_update_current_pm10(pm10_state)

        # if self.linked_pm25_sensor:
        #     pm25_serv = self.add_preload_service(
        #         SERV_AIR_QUALITY_SENSOR,
        #         [CHAR_AIR_QUALITY, CHAR_NAME, CHAR_PM25_DENSITY],
        #     )
        #     serv_air_purifier.add_linked_service(pm25_serv)
        #     pm25_serv.configure_char(
        #         CHAR_NAME, value=cleanup_name_for_homekit(f"{self.display_name} PM2.5")
        #     )
        #     self.char_pm25_density = pm25_serv.configure_char(
        #         CHAR_PM25_DENSITY, value=0
        #     )
        #
        #     self.char_pm25_air_quality = pm25_serv.configure_char(CHAR_AIR_QUALITY)
        #
        #     pm25_state = self.hass.states.get(self.linked_pm25_sensor)
        #     if pm25_state:
        #         self._async_update_current_pm25(pm25_state)

        # if self.linked_pm10_sensor:
        #     pm10_serv = self.add_preload_service(
        #         SERV_AIR_QUALITY_SENSOR,
        #         [CHAR_AIR_QUALITY, CHAR_NAME, CHAR_PM10_DENSITY],
        #     )
        #     serv_air_purifier.add_linked_service(pm10_serv)
        #     pm10_serv.configure_char(
        #         CHAR_NAME, value=cleanup_name_for_homekit(f"{self.display_name} PM10")
        #     )
        #     self.char_pm10_density = pm10_serv.configure_char(
        #         CHAR_PM10_DENSITY, value=0
        #     )
        #
        #     self.char_pm10_air_quality = pm10_serv.configure_char(CHAR_AIR_QUALITY)
        #
        #     pm10_state = self.hass.states.get(self.linked_pm10_sensor)
        #     if pm10_state:
        #         self._async_update_current_pm10(pm10_state)

        self.char_speed: Characteristic | None = None
        self.char_swing: Characteristic | None = None

        if CHAR_ROTATION_SPEED in self.chars:
            # Initial value is set to 100 because 0 is a special value (off). 100 is
            # an arbitrary non-zero value. It is updated immediately by async_update_state
            # to set to the correct initial value.
            self.char_speed = serv_air_purifier.configure_char(
                CHAR_ROTATION_SPEED,
                value=100,
                properties={PROP_MIN_STEP: percentage_step},
            )

        if CHAR_SWING_MODE in self.chars:
            self.char_swing = serv_air_purifier.configure_char(CHAR_SWING_MODE, value=0)

        if CHAR_LOCK_PHYSICAL_CONTROLS in self.chars:
            self.char_lock_physical_controls = serv_air_purifier.configure_char(
                CHAR_LOCK_PHYSICAL_CONTROLS, value=0
            )
            lock_physical_controls_switch_state = self.hass.states.get(
                self.linked_lock_physical_controls_switch
            )
            if lock_physical_controls_switch_state:
                self._async_update_lock_physical_controls_switch(
                    lock_physical_controls_switch_state
                )

        self.async_update_state(state)

        serv_air_purifier.setter_callback = self._set_chars

    @pyhap_callback  # type: ignore[misc]
    @callback
    def run(self) -> None:
        """Handle accessory driver started event.

        Run inside the Home Assistant event loop.
        """
        if self.linked_temperature_sensor:
            self._subscriptions.append(
                async_track_state_change_event(
                    self.hass,
                    [self.linked_temperature_sensor],
                    self._async_update_current_temperature_event,
                    job_type=HassJobType.Callback,
                )
            )

        if self.linked_humidity_sensor:
            self._subscriptions.append(
                async_track_state_change_event(
                    self.hass,
                    [self.linked_humidity_sensor],
                    self._async_update_current_humidity_event,
                    job_type=HassJobType.Callback,
                )
            )

        if self.linked_pm25_sensor:
            self._subscriptions.append(
                async_track_state_change_event(
                    self.hass,
                    [self.linked_pm25_sensor],
                    self._async_update_current_pm25_event,
                    job_type=HassJobType.Callback,
                )
            )

        if self.linked_pm10_sensor:
            self._subscriptions.append(
                async_track_state_change_event(
                    self.hass,
                    [self.linked_pm10_sensor],
                    self._async_update_current_pm10_event,
                    job_type=HassJobType.Callback,
                )
            )

        if self.linked_lock_physical_controls_switch:
            self._subscriptions.append(
                async_track_state_change_event(
                    self.hass,
                    [self.linked_lock_physical_controls_switch],
                    self._async_update_lock_physical_controls_switch_event,
                    job_type=HassJobType.Callback,
                )
            )

        super().run()

    @callback
    def _async_update_current_temperature_event(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle state change event listener callback."""
        self._async_update_current_temperature(event.data.get("new_state"))

    @callback
    def _async_update_current_temperature(self, new_state: State | None) -> None:
        """Handle linked temperature sensor state change to update HomeKit value."""
        if new_state is None:
            _LOGGER.error(
                "%s: Unable to update from linked temperature sensor %s: the entity state is None",
                self.entity_id,
                self.linked_temperature_sensor,
            )
            return
        try:
            current_temperature = float(new_state.state)
        except ValueError as ex:
            _LOGGER.debug(
                "%s: Unable to update from linked temperature sensor %s: %s",
                self.entity_id,
                self.linked_temperature_sensor,
                ex,
            )
            return
        if self.char_current_temperature.value != current_temperature:
            _LOGGER.debug(
                "%s: Linked temperature sensor %s changed to %d",
                self.entity_id,
                self.linked_temperature_sensor,
                current_temperature,
            )
            self.char_current_temperature.set_value(current_temperature)

    @callback
    def _async_update_current_humidity_event(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle state change event listener callback."""
        self._async_update_current_humidity(event.data.get("new_state"))

    @callback
    def _async_update_current_humidity(self, new_state: State | None) -> None:
        """Handle linked humidity sensor state change to update HomeKit value."""
        if new_state is None:
            _LOGGER.error(
                "%s: Unable to update from linked humidity sensor %s: the entity state is None",
                self.entity_id,
                self.linked_humidity_sensor,
            )
            return
        try:
            current_humidity = float(new_state.state)
        except ValueError as ex:
            _LOGGER.debug(
                "%s: Unable to update from linked humidity sensor %s: %s",
                self.entity_id,
                self.linked_humidity_sensor,
                ex,
            )
            return
        if self.char_current_humidity.value != current_humidity:
            _LOGGER.debug(
                "%s: Linked humidity sensor %s changed to %d",
                self.entity_id,
                self.linked_humidity_sensor,
                current_humidity,
            )
            self.char_current_humidity.set_value(current_humidity)

    @callback
    def _async_update_current_pm25_event(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle state change event listener callback."""
        self._async_update_current_pm25(event.data.get("new_state"))

    @callback
    def _async_update_current_pm25(self, new_state: State | None) -> None:
        """Handle linked pm25 sensor state change to update HomeKit value."""
        if new_state is None:
            _LOGGER.error(
                "%s: Unable to update from linked pm25 sensor %s: the entity state is None",
                self.entity_id,
                self.linked_pm25_sensor,
            )
            return
        try:
            current_pm25 = float(new_state.state)
        except ValueError as ex:
            _LOGGER.debug(
                "%s: Unable to update from linked pm25 sensor %s: %s",
                self.entity_id,
                self.linked_pm25_sensor,
                ex,
            )
            return
        if self.char_pm25_density.value != current_pm25:
            _LOGGER.debug(
                "%s: Linked pm25 sensor %s changed to %d",
                self.entity_id,
                self.linked_pm25_sensor,
                current_pm25,
            )
            self.char_pm25_density.set_value(current_pm25)
            self._update_air_quality()
            # air_quality = density_to_air_quality(current_pm25)
            # self.char_pm25_air_quality.set_value(air_quality)
            # _LOGGER.debug("%s: Set air_quality to %d", self.entity_id, air_quality)

    @callback
    def _async_update_current_pm10_event(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle state change event listener callback."""
        self._async_update_current_pm10(event.data.get("new_state"))

    @callback
    def _async_update_current_pm10(self, new_state: State | None) -> None:
        """Handle linked pm10 sensor state change to update HomeKit value."""
        if new_state is None:
            _LOGGER.error(
                "%s: Unable to update from linked pm10 sensor %s: the entity state is None",
                self.entity_id,
                self.linked_pm10_sensor,
            )
            return
        try:
            current_pm10 = float(new_state.state)
        except ValueError as ex:
            _LOGGER.debug(
                "%s: Unable to update from linked pm10 sensor %s: %s",
                self.entity_id,
                self.linked_pm10_sensor,
                ex,
            )
            return
        if self.char_pm10_density.value != current_pm10:
            _LOGGER.debug(
                "%s: Linked pm10 sensor %s changed to %d",
                self.entity_id,
                self.linked_pm10_sensor,
                current_pm10,
            )
            self.char_pm10_density.set_value(current_pm10)
            self._update_air_quality()
            # air_quality = density_to_air_quality_pm10(current_pm10)
            # self.char_pm10_air_quality.set_value(air_quality)
            # _LOGGER.debug("%s: Set air_quality to %d", self.entity_id, air_quality)

    def _update_air_quality(self) -> None:
        if self.char_pm25_density:
            current_pm25 = self.char_pm25_density.value
            air_quality_pm25 = density_to_air_quality(current_pm25)
        else:
            air_quality_pm25 = -1

        if self.char_pm10_density:
            current_pm10 = self.char_pm10_density.value
            air_quality_pm10 = density_to_air_quality_pm10(current_pm10)
        else:
            air_quality_pm10 = -1

        air_quality = max(air_quality_pm25, air_quality_pm10)

        self.char_air_quality.set_value(air_quality)
        _LOGGER.debug("%s: Set air_quality to %d", self.entity_id, air_quality)

    @callback
    def _async_update_lock_physical_controls_switch_event(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle state change event listener callback."""
        self._async_update_lock_physical_controls_switch(event.data.get("new_state"))

    @callback
    def _async_update_lock_physical_controls_switch(
        self, new_state: State | None
    ) -> None:
        """Handle linked lock physical controls switch state change to update HomeKit value."""
        if new_state is None:
            _LOGGER.error(
                "%s: Unable to update from linked lock physical controls switch %s: the entity state is None",
                self.entity_id,
                self.linked_lock_physical_controls_switch,
            )
            return
        lock_physical_controls = new_state.state == STATE_ON
        if self.char_lock_physical_controls.value != lock_physical_controls:
            _LOGGER.debug(
                "%s: Linked lock physical controls switch %s changed to %d",
                self.entity_id,
                self.linked_lock_physical_controls_switch,
                lock_physical_controls,
            )
            self.char_lock_physical_controls.set_value(lock_physical_controls)

    def _set_chars(self, char_values: dict[str, Any]) -> None:
        _LOGGER.debug("AirPurifier _set_chars: %s", char_values)
        if CHAR_ACTIVE in char_values:
            if char_values[CHAR_ACTIVE]:
                # If the device supports set speed we
                # do not want to turn on as it will take
                # the fan to 100% than to the desired speed.
                #
                # Setting the speed will take care of turning
                # on the fan if FanEntityFeature.SET_SPEED is set.
                if not self.char_speed or CHAR_ROTATION_SPEED not in char_values:
                    self.set_state(1)
            else:
                # Its off, nothing more to do as setting the
                # other chars will likely turn it back on which
                # is what we want to avoid
                self.set_state(0)
                return

        if CHAR_SWING_MODE in char_values:
            self.set_oscillating(char_values[CHAR_SWING_MODE])

        # We always do this LAST to ensure they
        # get the speed they asked for
        if CHAR_ROTATION_SPEED in char_values:
            self.set_percentage(char_values[CHAR_ROTATION_SPEED])

        if CHAR_LOCK_PHYSICAL_CONTROLS in char_values:
            self.set_lock_physical_controls(char_values[CHAR_LOCK_PHYSICAL_CONTROLS])

        if CHAR_TARGET_AIR_PURIFIER_STATE in char_values:
            self.set_single_preset_mode(char_values[CHAR_TARGET_AIR_PURIFIER_STATE])

    def set_single_preset_mode(self, value: int) -> None:
        """Set auto call came from HomeKit."""
        params: dict[str, Any] = {ATTR_ENTITY_ID: self.entity_id}
        if value and self.preset_auto:
            _LOGGER.debug("%s: Set auto to 1 (%s)", self.entity_id, self.preset_auto)
            params[ATTR_PRESET_MODE] = self.preset_auto
            self.async_call_service(FAN_DOMAIN, SERVICE_SET_PRESET_MODE, params)
        elif not value and self.preset_manual:
            _LOGGER.debug("%s: Set auto to 0 (%s)", self.entity_id, self.preset_manual)
            params[ATTR_PRESET_MODE] = self.preset_manual
            self.async_call_service(FAN_DOMAIN, SERVICE_SET_PRESET_MODE, params)

    def set_preset_mode(self, value: int, preset_mode: str) -> None:
        """Set preset_mode if call came from HomeKit."""
        _LOGGER.debug(
            "%s: Set preset_mode %s to %d", self.entity_id, preset_mode, value
        )
        params = {ATTR_ENTITY_ID: self.entity_id}
        if value:
            params[ATTR_PRESET_MODE] = preset_mode
            self.async_call_service(FAN_DOMAIN, SERVICE_SET_PRESET_MODE, params)
        # else:
        #     self.async_call_service(FAN_DOMAIN, SERVICE_TURN_ON, params)

    def set_state(self, value: int) -> None:
        """Set state if call came from HomeKit."""
        _LOGGER.debug("%s: Set state to %d", self.entity_id, value)
        service = SERVICE_TURN_ON if value == 1 else SERVICE_TURN_OFF
        params = {ATTR_ENTITY_ID: self.entity_id}
        self.async_call_service(FAN_DOMAIN, service, params)

    def set_oscillating(self, value: int) -> None:
        """Set state if call came from HomeKit."""
        _LOGGER.debug("%s: Set oscillating to %d", self.entity_id, value)
        oscillating = value == 1
        params = {ATTR_ENTITY_ID: self.entity_id, ATTR_OSCILLATING: oscillating}
        self.async_call_service(FAN_DOMAIN, SERVICE_OSCILLATE, params, oscillating)

    def set_percentage(self, value: float) -> None:
        """Set state if call came from HomeKit."""
        _LOGGER.debug("%s: Set speed to %d", self.entity_id, value)
        params = {ATTR_ENTITY_ID: self.entity_id, ATTR_PERCENTAGE: value}
        self.async_call_service(FAN_DOMAIN, SERVICE_SET_PERCENTAGE, params, value)

    def set_lock_physical_controls(self, value: int) -> None:
        """Set state if call came from HomeKit."""
        _LOGGER.debug("%s: Set lock physical controls to %d", self.entity_id, value)
        service = SERVICE_TURN_ON if value == 1 else SERVICE_TURN_OFF
        params = {ATTR_ENTITY_ID: self.linked_lock_physical_controls_switch}
        self.async_call_service(SWITCH_DOMAIN, service, params)

    @callback
    def async_update_state(self, new_state: State) -> None:
        """Update air purifier after state change."""
        # Handle State
        state = new_state.state
        attributes = new_state.attributes
        if state == STATE_ON:
            self.char_active.set_value(1)
        elif state == STATE_OFF:
            self.char_active.set_value(0)

        # Handle Speed
        if self.char_speed is not None and state != STATE_OFF:
            # We do not change the homekit speed when turning off
            # as it will clear the restore state
            percentage = attributes.get(ATTR_PERCENTAGE)
            # If the homeassistant component reports its speed as the first entry
            # in its speed list but is not off, the hk_speed_value is 0. But 0
            # is a special value in homekit. When you turn on a homekit accessory
            # it will try to restore the last rotation speed state which will be
            # the last value saved by char_speed.set_value. But if it is set to
            # 0, HomeKit will update the rotation speed to 100 as it thinks 0 is
            # off.
            #
            # Therefore, if the hk_speed_value is 0 and the device is still on,
            # the rotation speed is mapped to 1 otherwise the update is ignored
            # in order to avoid this incorrect behavior.
            if percentage == 0 and state == STATE_ON:
                percentage = max(1, self.char_speed.properties[PROP_MIN_STEP])
            if percentage is not None:
                self.char_speed.set_value(percentage)

        # Handle Oscillating
        if self.char_swing is not None:
            oscillating = attributes.get(ATTR_OSCILLATING)
            if isinstance(oscillating, bool):
                hk_oscillating = 1 if oscillating else 0
                self.char_swing.set_value(hk_oscillating)

        current_preset_mode = attributes.get(ATTR_PRESET_MODE)
        # Handle single preset mode
        # Automatic mode is represented in HASS by a preset called Auto or auto
        self.char_target_air_purifier_state.set_value(
            TARGET_STATE_AUTOMATIC
            if current_preset_mode and current_preset_mode == self.preset_auto
            else TARGET_STATE_MANUAL
        )
        # Handle multiple preset modes
        for preset_mode, char in self.preset_mode_chars.items():
            hk_value = 1 if preset_mode == current_preset_mode else 0
            char.set_value(hk_value)

        self.char_current_air_purifier_state.set_value(
            CURRENT_STATE_ACTIVE if state == STATE_ON else CURRENT_STATE_INACTIVE
        )
