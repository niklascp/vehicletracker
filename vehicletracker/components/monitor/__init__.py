"""Vehicle Tracker History Component"""

import logging
from datetime import datetime, timedelta
from typing import (Any, Dict)
import json
from shapely import wkt
from shapely.ops import linemerge

from vehicletracker.core import callback, VehicleTrackerNode
from vehicletracker.helpers.events import async_track_utc_time_change
from vehicletracker.helpers.datetime import utcnow, parse_datetime, as_local, as_utc
from vehicletracker.helpers.json import DateTimeEncoder

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'monitor'

async def async_setup(node : VehicleTrackerNode, config : Dict[str, Any]):    
    """Setup monitor component"""

    monitor = node.data[DOMAIN] = Monitor(node, config[DOMAIN])

    await node.events.async_listen('vehicleJourneyAssignment', monitor.vehicle_journey_assignment)
    
    await node.events.async_listen('linkCompleted', monitor.link_completed)
    await node.events.async_listen('departure', monitor.updated_departure)
    await node.events.async_listen('estimated_departure', monitor.updated_departure)
    await node.events.async_listen('arrival', monitor.updated_arrival)
    await node.events.async_listen('estimated_arrival', monitor.updated_arrival)

    await node.services.async_register(DOMAIN, 'journeys', monitor.list_journeys)
    await node.services.async_register(DOMAIN, 'journey_details', monitor.journey_details)
    await node.services.async_register(DOMAIN, 'stop_points', monitor.list_stop_points)
    await node.services.async_register(DOMAIN, 'link_geometries', monitor.list_link_geometries)
    node.async_add_job(monitor.fetch_stop_points, utcnow())
    node.async_add_job(monitor.fetch_journeys, utcnow())
    await async_track_utc_time_change(node, monitor.fetch_journeys, hour='*', minute='*', second=0)

    return True

class Monitor():

    def __init__(self, node : VehicleTrackerNode, config : Dict[str, Any]):
        self.node = node

        try:
            with open('cache/journeys.json', 'r') as f:
                self.journey_map = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e: 
            _LOGGER.warning('Failed to restore journey cache: %s', e)
            self.journey_map = {}

        self.stop_points = {}
        self.link_geometries = {}
        self._json_encoder = DateTimeEncoder()

    def vehicle_journey_assignment(self, event_type, event_data):
        """Event handler for 'vehicleJourneyAssignment'. Add vehicle information to journey."""

        journey_ref = event_data['vehicleJourneyAssignment']['journeyRef']
        vehicle_ref = event_data['vehicleJourneyAssignment']['vehicleRef']
        valid_from_utc = event_data['vehicleJourneyAssignment']['validFromUtc']
        invalid_from_utc = event_data['vehicleJourneyAssignment']['invalidFromUtc']

        if journey_ref in self.journey_map:
            journey = self.journey_map[journey_ref]
            journey['vehicleRef'] = vehicle_ref
            journey['vehicleValidFromUtc'] = valid_from_utc
            journey['vehicleValidToUtc'] = invalid_from_utc

            if invalid_from_utc and journey['linkIndex'] >= len(journey['links']) - 2:
                journey['state'] = 'Completed'

    def link_completed(self, event_type, event_data):
        """Event handler for 'linkCompleted'. Collects true link time for error calculation."""

        journey_ref = event_data['journeyRef']
        sequence_number = event_data['sequenceNumber']
        link_ref = event_data['linkRef']
        if journey_ref in self.journey_map:
            journey = self.journey_map[journey_ref]
            journey['vehicleRef'] = event_data['vehicleRef']
            try:
                ix, link = next((ix, ln) for (ix, ln) in enumerate(journey['links']) if ln['sequenceNumber'] == sequence_number)
                if link['linkRef'] != link_ref:
                    _LOGGER.warning(
                        'Link refs dot not match for journey: %s, sequence_number: %s. '
                        'Expected %s, found %s.',
                        journey_ref, sequence_number, link_ref, link['linkRef'])
                    return
                link['vehicleRef'] = event_data['vehicleRef']
                link['observedTime'] = event_data['travelTimeSeconds']

                if link.get('predictions'):
                    for pred in link['predictions']:
                        pred['error'] = event_data['travelTimeSeconds'] - pred['predicted']                        

                if link.get('predictedTime'):
                    link['error'] = event_data['travelTimeSeconds'] - link['predictedTime']
                    link['errorAcc'] = journey.get('linkErrorAcc', 0) + link['error']
                    journey['linkErrorAcc'] = journey.get('linkErrorAcc', 0) + link['error']

                # Progress the link one ahead
                if ix + 1 < len(journey['links']):
                    journey['linkIndex'] = ix + 1
                journey['currentDistance'] = link['totalDistance']
            except StopIteration:
                _LOGGER.warning(
                    'Could not find link for journey: %s, sequence_number: %s.',
                    journey_ref, sequence_number)    

    def updated_departure(self, event_type, event_data):
        journey_ref = event_data['journeyRef']
        sequence_number = event_data['sequenceNumber']
        departure_time = parse_datetime(event_data['observedUtc']) if event_type == 'departure' else event_data['estimatedUtc']
        journey = self.journey_map.get(journey_ref)
        
        if journey is None:
            return    

        try:
            link = next(ln for ln in journey['links'] if ln['sequenceNumber'] >= sequence_number)
            stop = next(sp for sp in journey['stops'] if sp['sequenceNumber'] >= sequence_number)
        except StopIteration:
            _LOGGER.warning(
                'Could not find link/stop for journey: %s, sequence_number: %s.',
                journey_ref, sequence_number) 
            return 

        link_predictions = self.node.services.call('link_predict', { 'linkRef': link['linkRef'], 'time': as_local(departure_time) }) 

        if len(link_predictions) == 0:
            # Fallback to timetable
            predicted = link['plannedTime']
        else:
            predicted = link_predictions[0]['predicted'] #TODO: Just dont take the first!

        # Actual departure
        if event_type == 'departure':
            journey['state'] = 'Run'        
            stop['observedDepartureUtc'] = departure_time
            journey['delay'] = (departure_time - parse_datetime(stop['plannedDepartureUtc'])).total_seconds()

        link['predictedTime'] = predicted
        link['predictedUpdated'] = utcnow()
        link['predictions'] = link_predictions

        self.node.events.publish_local('estimated_arrival', {
            'journeyRef': journey_ref,
            'sequenceNumber': sequence_number + 1, #TODO: Not always correct!
            'estimatedUtc': departure_time + timedelta(seconds=predicted)
        })

    def updated_arrival(self, event_type, event_data): 
        """Event handler for 'arrival' and 'estimated_arrival'. Predicts dwell time and cascade downstream via estimated_departure event."""
        
        journey_ref = event_data['journeyRef']
        sequence_number = event_data['sequenceNumber']
        arrival_time = parse_datetime(event_data['observedUtc']) if event_type == 'arrival' else event_data['estimatedUtc']
        journey = self.journey_map.get(journey_ref)
        
        if journey is None:
            return

        if event_type == 'arrival' and event_data['state'] == 'ARRIVED':
            journey['state'] = 'Dwell'

        try:
            ix, stop = next((ix, ln) for (ix, ln) in enumerate(journey['stops']) if ln['sequenceNumber'] >= sequence_number)

            predicted = 0

            if event_type == 'arrival':
                stop['observedArrivalUtc'] = arrival_time
                #ignore passed arrivals, update will cascade from the corresponding departure.
                if event_data['state'] == 'PASSED':
                    return
            else:
                stop['predictedArrivalUtc'] = arrival_time
            
            stop['predictedDwellTime'] = predicted
            stop['predictedDepartureUtc'] = arrival_time + timedelta(seconds=predicted)
            stop['predictedUpdated'] = utcnow()

            if ix < len(journey['stops']) - 1:
                # This is not the last stop: Emit estimated departure
                self.node.events.publish_local('estimated_departure', {
                    'journeyRef': journey_ref,
                    'sequenceNumber': sequence_number,
                    'estimatedUtc': arrival_time + timedelta(seconds=predicted)
                })

        except StopIteration:
            _LOGGER.warning(
                'Could not find link for journey: %s, sequence_number: %s.',
                journey_ref, sequence_number)    

    # Helper API methods

    def journey_details(self, service_data):
        if 'journeyRef' in service_data:
            journey_ref = service_data['journeyRef']
            return self.journey_map.get(journey_ref)
        return None

    def list_journeys(self, service_data):
        return [
            {
                k: v.get(k)
                for k in ['journeyRef', 'lineDesignation', 'plannedStartDateTime', 'plannedEndDateTime',
                          'origin', 'destination', 'vehicleRef', 'state', 'delay', 'currentDistance', 'totalDistance']
            }
            for v in self.journey_map.values()
        ]

    def list_stop_points(self, service_data):
        """Returns stop point meta data, mainly used for map display."""
        if 'journeyRef' in service_data:
            # Return stop points meta data for specific journey
            journey_ref = service_data['journeyRef']
            if journey_ref in self.journey_map:
                return [
                    self.stop_points.get(str(x['stopPointRef']))
                    for x in self.journey_map[journey_ref]['stops']
                ]
        else:
            # Return meta data for all stop points
            return list(self.stop_points.values())
        return None

    def list_link_geometries(self, service_data):
        """Returns stop point meta data, mainly used for map display."""
        def link_geometry_view(link_ref):
            if not link_ref in  self.link_geometries:
                return None

            link_geometry = self.link_geometries[link_ref]
            geom = link_geometry['geometry']
            if geom.geom_type == 'MultiLineString':
                multicoords = [list(line.coords) for line in geom]
                coords = [item for sublist in multicoords for item in sublist]
            else:
                coords = geom.coords

            return {
                'linkRef': link_geometry['linkRef'],
                'coords': [[y, x] for [x, y] in coords]
            }

        if 'journeyRef' in service_data:
            # Return stop points meta data for specific journey
            journey_ref = service_data['journeyRef']
            if journey_ref in self.journey_map:
                return [
                    link_geometry_view(x['linkRef'])
                    for x in self.journey_map[journey_ref]['links']
                ]
        else:
            # Return meta data for all links
            return [
                link_geometry_view(x['linkRef'])
                for x in self.link_geometries.keys()
            ]
        return None

    # Background worker methods

    def fetch_stop_points(self, utc_time):   
        self.stop_points = {str(x['stopPointRef']): x for x in self.node.services.call('load_stop_points')}

    def fetch_journeys(self, utc_time):        
        journeys = self.node.services.call('load_journeys', { 'fromDateTime': '?' })

        new_journeys = 0
        for journey in journeys:
            if not journey['journeyRef'] in self.journey_map:
                journey['stops'] = self.node.services.call('load_journey_stops', { 'journeyRef': journey['journeyRef'] })
                journey['links'] = self.node.services.call('load_journey_links', { 'journeyRef': journey['journeyRef'] })
                journey['added'] = utc_time
                journey['totalDistance'] = journey['links'][-1]['totalDistance']                
                self.journey_map[journey['journeyRef']] = journey
                new_journeys += 1

        removed_journey = 0
        horizon = datetime.now() - timedelta(minutes=15)
        for k in list(self.journey_map.keys()):
            journey = self.journey_map[k]
            
            if parse_datetime(journey['plannedEndDateTime']) < horizon:
                del self.journey_map[k]
                removed_journey += 1
                continue

            if any([not link['linkRef'] in self.link_geometries for link in journey['links']]):
                journey_link_geometries = self.node.services.call('load_link_geometry', { 'journeyRef': journey['journeyRef'] })
                for link_geometry in journey_link_geometries:
                    link_ref = link_geometry['linkRef']
                    if not link_ref in self.link_geometries:
                        link_geometry['geometry'] = wkt.loads(link_geometry['geometryWkt'])
                        self.link_geometries[link_ref] = link_geometry

        _LOGGER.info('Loaded %s new journeys, removed %s journeys', new_journeys, removed_journey)
        # Dump cache if we stop...
        with open('cache/journeys.json', 'w') as f:
            f.write(self._json_encoder.encode(self.journey_map))
