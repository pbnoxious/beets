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
        })

        self._command = ui.Subcommand('lastimportlastplayed', help=u'import last.fm last_played times')
        self._command.parser.add_option(
            u'-f', u'--from', dest='time_from', type=int,
            help=u'time from which play dates will be imported as UNIX timestamp',
        )
        self._command.parser.add_option(
            u'-t', u'--to', dest='time_to', type=int,
            help=u'time until which play dates will be imported as UNIX timestamp',
        )

    def commands(self):

        def func(lib, opts, args):
            self.config.set_args(opts)
            time_from = self.config['time_from'].get()
            time_to = self.config['time_to'].get()
            import_lastfm_last_played(lib, self._log, time_from=time_from, time_to=time_to)

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


def import_lastfm_last_played(lib, log, time_from, time_to):
    user = config['lastfm']['user'].as_str()
    per_page = config['lastimportlastplayed']['per_page'].get(int)

    if not user:
        raise ui.UserError(u'You must specify a user name for lastimportlastplayed')

    log.info(u'Fetching recent tracks from last.fm for @{0}', user)

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
            tracks, page_total = fetch_tracks(user, page_current + 1, per_page,
                                              time_from, time_to)
            if page_total < 1:
                # It means nothing to us!
                raise ui.UserError(u'Last.fm reported no data.')

            if tracks:
                found, not_updated, unknown = process_tracks(lib, tracks, log)
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


def process_tracks(lib, tracks, log):
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
            song = lib.items(query).get()

        # If not, try just artist/title
        if song is None:
            log.debug(u'no album match, trying by only artist/title')
            query = dbcore.AndQuery([
                dbcore.query.SubstringQuery('artist', artist),
                dbcore.query.SubstringQuery('title', title)
            ])
            song = lib.items(query).get()

        # Last resort, try just replacing to utf-8 quote
        if song is None:
            title = title.replace("'", u'\u2019')
            log.debug(u'no title match, trying utf-8 single quote')
            query = dbcore.AndQuery([
                dbcore.query.SubstringQuery('artist', artist),
                dbcore.query.SubstringQuery('title', title)
            ])
            song = lib.items(query).get()

        # if fuzzy query is installed: try first with fuzzy title
        if song is None and FUZZY_AVAIL:
            log.debug(u'no match, trying fuzzy search')
            query = dbcore.AndQuery([
                dbcore.query.SubstringQuery('artist', artist),
                FuzzyQuery('title', title)
            ])
            results = lib.items(query)
            # then also with artist
            if results is None:
                query = dbcore.AndQuery([
                    FuzzyQuery('artist', artist),
                    FuzzyQuery('title', title)
                ])
                results = lib.items(query)
            if results is not None:
                if len(results) == 1:
                    song = results[0]
                else:
                    song = select_result(results, lib)

        if song is not None:
            last_played = int(song.get('last_played', 0))
            if last_played < timestamp:
                log.debug(u'match: {0} - {1} ({2}) '
                          u'updating: last_played {3} => {4}',
                          song.artist, song.title, song.album, last_played, timestamp)
                song['last_played'] = timestamp
                song.store()
                total_found += 1
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
        log.info(u'Updated {0}/{1})',
                 total_found, total, total_fails)

    return total_found, not_updated, total_fails

def select_result(results, lib):
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
            return None
        if choice == "e":
            song = user_query(lib, True)
            return song
        try:
            choice = int(choice)
        except ValueError as e:
            print(u'Input could not be parsed correctly: {}'.format(choice))
            continue
        if choice >= num_res or choice < 0:
            print(u'Input number is not one of the given choices')
            continue
        else:
            return results[choice]


def user_query(lib, do_query=False):
    if not do_query:
        do_query = input(u'Manual query? y / [n]: ').lower()
    if do_query:
        query_str = input(u'Enter custom query string: ')
        query, sort = library.parse_query_string(query_str, library.Item)
        return select_result(lib.items(query, sort), lib)
