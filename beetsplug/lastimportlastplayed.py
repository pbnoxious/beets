# -*- coding: utf-8 -*-
# This file is part of beets.
# Copyright 2020, pbnoxious
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

from __future__ import division, absolute_import, print_function
from sqlite3 import OperationalError
import time

import pylast
from pylast import _extract
from beets import ui
from beets import dbcore
from beets import config
from beets import plugins
from beets import library

try:
    from beetsplug.fuzzy import FuzzyQuery
    FUZZY_AVAIL = True
except ImportError:
    FUZZY_AVAIL = False



API_URL = 'https://ws.audioscrobbler.com/2.0/'


class LastImportPlugin(plugins.BeetsPlugin):
    def __init__(self):
        super(LastImportPlugin, self).__init__()
        config['lastfm'].add({
            'user':     '',
            'api_key':  plugins.LASTFM_KEY,
        })
        config['lastfm']['api_key'].redact = True
        self.config.add({
            'per_page': 50,
            'retry_limit': 3,
            'time_from': None,
            'time_to': None,
            'ask_user_query': True,
        })

        self._command = ui.Subcommand('lastimportlastplayed', help=u'import last.fm last_played times')
        self._command.parser.add_option(
            u'-f', u'--from', dest='time_from',
            help=u'time from which play dates will be imported as UNIX timestamp',
        )
        self._command.parser.add_option(
            u'-t', u'--to', dest='time_to',
            help=u'time until which play dates will be imported as UNIX timestamp',
        )

    def commands(self):

        def func(lib, opts, args):
            self.config.set_args(opts)
            time_from = self.config['time_from'].get()
            time_to = self.config['time_to'].get()
            ask_user_query = self.config['ask_user_query'].get()
            import_lastfm_last_played(lib, self._log, time_from=time_from, time_to=time_to, ask_user_query=ask_user_query)

        self._command.func = func
        return [self._command]


class CustomUser(pylast.User):
    """ Custom user class derived from pylast.User to add the get_recent_tracks_by_page
    method. Otherwise copied from lastimport.
    """
    def __init__(self, *args, **kwargs):
        super(CustomUser, self).__init__(*args, **kwargs)

    def get_recent_tracks_by_page(self, limit=50, page=1, cacheable=True,
                                  time_from=None, time_to=None):
        """
        Get recent tracks page wise instead of all at once to avoid too large queries.
        Works otherwise just like the get_recent_tracks() method of the base class

        Parameters:
        limit : If None, it will try to pull all the available data.
        from (Optional) : Beginning timestamp of a range - only display
        scrobbles after this time, in UNIX timestamp format (integer
        number of seconds since 00:00:00, January 1st 1970 UTC). This
        must be in the UTC time zone.
        to (Optional) : End timestamp of a range - only display scrobbles
        before this time, in UNIX timestamp format (integer number of
        seconds since 00:00:00, January 1st 1970 UTC). This must be in
        the UTC time zone.

        Returns:
        seq : list of pylast.PlayedTrack elements
        total_pages : total number of pages in API call
        """

        params = self._get_params()
        params['limit'] = limit
        params['page'] = page
        if time_from:
            params["from"] = time_from
        if time_to:
            params["to"] = time_to

        doc = self._request(
            self.ws_prefix + ".getRecentTracks", cacheable, params)
        recenttracks_node = doc.getElementsByTagName('recenttracks')[0]
        total_pages = int(recenttracks_node.getAttribute('totalPages'))

        seq = []
        for track in doc.getElementsByTagName('track'):
            if track.hasAttribute("nowplaying"):
                continue  # to prevent the now playing track from sneaking in

            title = _extract(track, "name")
            artist = _extract(track, "artist")
            album = _extract(track, "album")
            mbid = _extract(track, "mbid")
            date = _extract(track, "date")
            timestamp = track.getElementsByTagName("date")[0].getAttribute("uts")

            tr = pylast.Track(artist, title, self.network)
            tr.mbid = mbid
            seq.append(pylast.PlayedTrack(tr, album, date, timestamp))

        return seq, total_pages


def import_lastfm_last_played(lib, log, time_from, time_to, ask_user_query):
    user = config['lastfm']['user'].as_str()
    per_page = config['lastimportlastplayed']['per_page'].get(int)

    if not user:
        raise ui.UserError(u'You must specify a user name for lastimportlastplayed')

    time_from_stamp = parse_time(time_from)
    time_to_stamp = parse_time(time_to)
    log.info(u'Fetching tracks from last.fm for @{0} between {1} and {2}',
             user, time_from_stamp, time_to_stamp)

    page_total = 1
    page_current = 0
    found_total = 0
    not_updated_total = 0
    unknown_total = 0
    retry_limit = config['lastimport']['retry_limit'].get(int)
    # Iterate through a yet to be known page total count
    while page_current < page_total:
        log.info(u'Querying page #{0}{1}...',
                 page_current + 1,
                 '/{}'.format(page_total) if page_total > 1 else '')

        for retry in range(0, retry_limit):
            tracks = None
            try:
                tracks, page_total = fetch_tracks(user, page_current + 1, per_page,
                                                  time_from_stamp, time_to_stamp)
            except pylast.WSError:
                log.info(u'Could not get data from Last.fm web service.')

            if page_total < 1 and retry == retry_limit:
                # It means nothing to us!
                raise ui.UserError(u'Last.fm reported no data.')

            if tracks:
                found, not_updated, unknown = process_tracks(lib, tracks, log, ask_user_query)
                found_total += found
                not_updated_total += not_updated
                unknown_total += unknown
                break
            else:
                log.error(u'ERROR: unable to read page #{0}',
                          page_current + 1)
                if retry < retry_limit:
                    log.info(
                        u'Retrying page #{0}... ({1}/{2} retry)',
                        page_current + 1, retry + 1, retry_limit
                    )
                else:
                    log.error(u'FAIL: unable to fetch page #{0}, ',
                              u'tried {1} times', page_current, retry + 1)
        page_current += 1

    log.info(u'... done!')
    log.info(u'finished processing recent played tracks pages', page_total)
    log.info(u'{0} not updated tracks', not_updated_total)
    log.info(u'{0} unknown tracks', unknown_total)
    log.info(u'{0} last_played tags updated', found_total)


def fetch_tracks(user, page, limit, time_from, time_to):
    """ JSON format:
        [
            {
                "pylast.Track": "...",
                "album": "...",
                "playback_date": "...",
            }
        ]
    """
    network = pylast.LastFMNetwork(api_key=config['lastfm']['api_key'])
    user_obj = CustomUser(user, network)
    results, total_pages =\
        user_obj.get_recent_tracks_by_page(limit, page=page,
                                           time_from=time_from, time_to=time_to)
    return results, total_pages


def process_tracks(lib, tracks, log, ask_user_query):
    total = len(tracks)
    total_found = 0
    not_updated = 0
    total_fails = 0
    log.info(u'Received {0} tracks in this page, processing...', total)

    for t in tracks:
        if not t.timestamp:
            continue

        song = None
        trackid = t.track.mbid.strip() if t.track.mbid else ''
        artist = t.track.artist.name.strip() if t.track.artist.name else ''
        title = t.track.title.strip() if t.track.title else ''
        album = t.album.strip() if t.album else None
        timestamp = int(t.timestamp)

        log.debug(u'query: {0} - {1} ({2})', artist, title, album)

        # First try to query by musicbrainz's trackid
        if trackid:
            song = lib.items(
                dbcore.query.MatchQuery('mb_trackid', trackid)
            ).get()

        # If not, try album, artist and title
        if song is None and album:
            log.debug(u'no id match, trying by artist, album and title')
            query = dbcore.AndQuery([
                dbcore.query.SubstringQuery('artist', artist),
                dbcore.query.SubstringQuery('album', album),
                dbcore.query.SubstringQuery('title', title)
            ])
            results = lib.items(query)
            song, skip = select_result(results, lib)

        # If not, try just artist/title
        if song is None and not skip:
            log.debug(u'no album match, trying by only artist/title')
            query = dbcore.AndQuery([
                dbcore.query.SubstringQuery('artist', artist),
                dbcore.query.SubstringQuery('title', title)
            ])
            results = lib.items(query)
            song, skip = select_result(results, lib)

        # Last resort, try just replacing to utf-8 quote
        if song is None and not skip:
            title = title.replace("'", u'\u2019')
            log.debug(u'no title match, trying utf-8 single quote')
            query = dbcore.AndQuery([
                dbcore.query.SubstringQuery('artist', artist),
                dbcore.query.SubstringQuery('title', title)
            ])
            results = lib.items(query)
            song, skip = select_result(results, lib)

        # if fuzzy query is installed: try first with fuzzy title
        if song is None and FUZZY_AVAIL and not skip:
            log.debug(u'no match, trying fuzzy search on title')
            query = dbcore.AndQuery([
                dbcore.query.SubstringQuery('artist', artist),
                FuzzyQuery('title', title)
            ])
            results = lib.items(query)
            song, skip = select_result(results, lib)
        # same with artist artist
        if song is None and FUZZY_AVAIL and not skip:
            log.debug(u'no match, trying fuzzy search on artist')
            query = dbcore.AndQuery([
                FuzzyQuery('artist', artist),
                dbcore.query.SubstringQuery('title', title),
            ])
            results = lib.items(query)
            song, skip = select_result(results, lib)
        # now with both
        if song is None and FUZZY_AVAIL and not skip:
            log.debug(u'no match, trying fuzzy search on both')
            query = dbcore.AndQuery([
                FuzzyQuery('artist', artist),
                FuzzyQuery('title', title)
            ])
            results = lib.items(query)
            song, skip = select_result(results, lib)

        if song is None and ask_user_query and not skip:
            song, skip = user_query(lib, True)

        if song is not None:
            last_played = int(song.get('last_played', 0))
            if last_played < timestamp:
                log.debug(u'match: {0} - {1} ({2}) '
                          u'updating: last_played {3} => {4}',
                          song.artist, song.title, song.album, last_played, timestamp)
                while True:
                    try:
                        song['last_played'] = timestamp
                        song.store()
                        total_found += 1
                        break
                    except OperationalError: # when database is locked
                        time.sleep(3)
            else:
                log.debug(u'match: {0} - {1} ({2}) '
                          u'not updating: {3} is not newer than current {4}',
                          song.artist, song.title, song.album, timestamp, last_played)
                not_updated += 1
        else:
            total_fails += 1
            log.info(u'  - No match: {0} - {1} ({2})',
                     artist, title, album)

    if total_fails > 0:
        log.info(u'Updated {0} of {1}',
                 total_found, total)

    return total_found, not_updated, total_fails

def select_result(results, lib, ask_query=False):
    """Check a Result instance from a query and let the user decide what to do next

    :return song: database track matching query
    :return skip: skip other queries for current track
    """
    if len(results) == 0:
        if ask_query:
            return user_query(lib, True)
        return None, False

    if len(results) == 1:
        return results[0], False

    num_res = len(results)
    print(u'{0} matches for query, choose one:'.format(num_res))
    for i, r in enumerate(results):
        print(u'[{num}] {artist} - {track} - {title} ({album})'.format(
            num=i, artist=r['artist'], track=r['track'], title=r['title'], album=r['album']))
    song = None
    while song is None:
        choice = input(u'Your choice? (0 - {0}, "s" to skip, "e" for custom query):\n'
                       .format(num_res - 1))
        if choice == "s":
            return None, True
        if choice == "e":
            return user_query(lib, False)
        try:
            choice = int(choice)
        except ValueError:
            print(u'Input could not be parsed correctly: {}'.format(choice))
            continue
        if choice >= num_res or choice < 0:
            print(u'Input number is not one of the given choices')
            continue
        song = results[choice]
    return song, False


def user_query(lib, ask_query=True):
    if ask_query:
        str_do_query = input(u'No matches, manual query? y / [n]: ').lower()
        if str_do_query != 'y':
            return None, False
    query_str = input(u'Enter custom query string: ')
    query, sort = library.parse_query_string(query_str, library.Item)
    return select_result(lib.items(query, sort), lib, True)


def parse_time(timestr):
    """Transform a date/time given as string to a timestamp"""
    try:  # was given as int
        return int(timestr)
    except ValueError:
        try:  # youtube API doesn't allow floats, so cut off precision
            return int(float(timestr))
        except ValueError:
            period = dbcore.query.Period.parse(timestr)
            return int(period.date.timestamp())
