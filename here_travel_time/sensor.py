import logging
import re
from datetime import timedelta, datetime

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.sensor import DOMAIN, PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_NAME, EVENT_HOMEASSISTANT_START, ATTR_LATITUDE,
    ATTR_LONGITUDE)
from homeassistant.helpers import location
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle

COORD_PATTERN = re.compile(r'([0-9]+(?:\.[0-9]+)?),([0-9]+(?:\.[0-9]+)?)')

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=5)

REQUIREMENTS = ['requests==2.21.0', 'isodate==0.6.0', 'geopy==1.18.1']

_LOGGER = logging.getLogger(__name__)

CONF_DESTINATION = 'destination'
CONF_ORIGIN = 'origin'
CONF_APP_ID = 'app_id'
CONF_APP_CODE = 'app_code'
CONF_MIN_DISTANCE = 'minimum_distance'

DEFAULT_NAME = 'Here Travel Time'

TRACKABLE_DOMAINS = ['device_tracker', 'sensor', 'zone']
DATA_KEY = 'here_travel_time'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_APP_ID): cv.string,
    vol.Required(CONF_APP_CODE): cv.string,
    vol.Required(CONF_DESTINATION): cv.string,
    vol.Required(CONF_ORIGIN): cv.string,
    vol.Optional(CONF_NAME): cv.string,
    vol.Optional(CONF_MIN_DISTANCE, default=100): cv.positive_int,
})


def setup_platform(hass, config, add_devices):
    """Setup """

    def run_setup(event):
        if DATA_KEY not in hass.data:
            hass.data[DATA_KEY] = []
            hass.services.register(
                DOMAIN, 'here_travel_sensor_update', update)

        name = config.get(CONF_NAME, 'Here Public Transport')
        app_id = config.get(CONF_APP_ID)
        app_code = config.get(CONF_APP_CODE)
        origin = config.get(CONF_ORIGIN)
        destination = config.get(CONF_DESTINATION)
        min_distance = config.get(CONF_MIN_DISTANCE)

        _LOGGER.debug('origin: {}, destination: {}'.format(origin, destination))

        sensor = HereTravelTimeSensor(hass, name, app_id, app_code, origin, destination, min_distance)
        hass.data[DATA_KEY].append(sensor)

        if sensor.valid_api_connection:
            add_devices([sensor])

    def update(service):
        """Update service for manual updates."""
        entity_id = service.data.get('entity_id')
        for sensor in hass.data[DATA_KEY]:
            if sensor.entity_id == entity_id:
                sensor.update(no_throttle=True)
                sensor.schedule_update_ha_state()

    # Wait until start event is sent to load this component.
    hass.bus.listen_once(EVENT_HOMEASSISTANT_START, run_setup)


class HereTravelTimeSensor(Entity):
    def __init__(self, hass, name, app_id, app_code, origin, destination, min_distance):
        self._hass = hass
        self._name = name
        self._app_id = app_id
        self._app_code = app_code
        self._unit_of_measurement = 'min'
        self._state = None
        self._min_distance = min_distance
        self.valid_api_connection = True

        # Check if location is a trackable entity
        if origin.split('.', 1)[0] in TRACKABLE_DOMAINS:
            self._origin_entity_id = origin
        else:
            self._origin = origin

        if destination.split('.', 1)[0] in TRACKABLE_DOMAINS:
            self._destination_entity_id = destination
        else:
            self._destination = destination

        try:
            self.update()
        except Exception as exc:
            _LOGGER.error(exc)
            self.valid_api_connection = False

    def get_lat_long(self, string_value):
        m = COORD_PATTERN.match(string_value)
        lat = float(m.group(1))
        long = float(m.group(2))
        return lat, long

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return self._unit_of_measurement

    @Throttle(min_time=MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        if hasattr(self, '_origin_entity_id'):
            self._origin = self._get_location_from_entity(
                self._origin_entity_id
            )

        if hasattr(self, '_destination_entity_id'):
            self._destination = self._get_location_from_entity(
                self._destination_entity_id
            )

        self._destination = self._resolve_zone(self._destination)
        self._origin = self._resolve_zone(self._origin)

        if self._destination is not None and self._origin is not None:
            # check if origin and destination are too close
            import geopy.distance
            origin_lat, origin_long = self.get_lat_long(self._origin)
            dest_lat, dest_long = self.get_lat_long(self._destination)
            distance = geopy.distance.vincenty((origin_lat, origin_long), (dest_lat, dest_long)).meters

            if distance < self._min_distance:
                _LOGGER.debug('Distance between origin and destination is only {}, skipping', distance)
                self._state = None
                return

            import requests as req
            response = req.get('https://transit.api.here.com/v3/route.json', params={
                'app_id': self._app_id,
                'app_code': self._app_code,
                'routing': 'all',
                'dep': self._origin,
                'arr': self._destination,
                'time': datetime.now().isoformat(),
                'max': 1,
                'details': 0
            })

            if response is not None:
                d = response.json()
                _LOGGER.debug(d)
                import isodate
                if 'Res' not in d or 'Connections' not in d['Res'] or 'Connection' not in d['Res']['Connections']:
                    self._state = None
                    return

                if len(d['Res']['Connections']['Connection']) == 0 or 'duration' not in \
                        d['Res']['Connections']['Connection'][0]:
                    self._state = None
                    return

                self._state = round(
                    isodate.parse_duration(d['Res']['Connections']['Connection'][0]['duration']).seconds / 60)

    def _get_location_from_entity(self, entity_id):
        """Get the location from the entity state or attributes."""
        entity = self._hass.states.get(entity_id)

        if entity is None:
            _LOGGER.error("Unable to find entity %s", entity_id)
            self.valid_api_connection = False
            return None

        # Check if the entity has location attributes
        if location.has_location(entity):
            return self._get_location_from_attributes(entity)

        # Check if device is in a zone
        zone_entity = self._hass.states.get("zone.%s" % entity.state)
        if location.has_location(zone_entity):
            _LOGGER.debug(
                "%s is in %s, getting zone location",
                entity_id, zone_entity.entity_id
            )
            return self._get_location_from_attributes(zone_entity)

        # If zone was not found in state then use the state as the location
        if entity_id.startswith("sensor."):
            return entity.state

        # When everything fails just return nothing
        return None

    @staticmethod
    def _get_location_from_attributes(entity):
        """Get the lat/long string from an entities attributes."""
        attr = entity.attributes
        return "%s,%s" % (attr.get(ATTR_LATITUDE), attr.get(ATTR_LONGITUDE))

    def _resolve_zone(self, friendly_name):
        entities = self._hass.states.all()
        for entity in entities:
            if entity.domain == 'zone' and entity.name == friendly_name:
                return self._get_location_from_attributes(entity)

        return friendly_name
