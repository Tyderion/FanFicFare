from datetime import datetime, timedelta

import logging
logger = logging.getLogger(__name__)

UNIX_EPOCHE = datetime.fromtimestamp(0)

## Currently used by adapter_webnovelcom & adapter_wwwnovelallcom

def parse_relative_date_string(string_):
    # Keep this explicit instead of replacing parentheses in case we
    # discover a format that is not so easily translated as a
    # keyword-argument to timedelta. In practice I have only observed
    # hours, weeks and days
    unit_to_keyword = {
        'second(s)': 'seconds',
        'minute(s)': 'minutes',
        'hour(s)': 'hours',
        'day(s)': 'days',
        'week(s)': 'weeks',
        'seconds': 'seconds',
        'minutes': 'minutes',
        'hours': 'hours',
        'days': 'days',
        'weeks': 'weeks',
        'second': 'seconds',
        'minute': 'minutes',
        'hour': 'hours',
        'day': 'days',
        'week': 'weeks',
    }

    # discards trailing ' ago' if present
    value, unit_string = string_.split()[:2]
    unit = unit_to_keyword.get(unit_string)
    if not unit:
        ## I'm not going to worry very much about accuracy for a site
        ## that considers '2 years ago' and acceptable time stamp.
        if "year" in unit_string:
            value = unicode(int(value)*365)
            unit = 'days'
        elif "month" in unit_string:
            value = unicode(int(value)*31)
            unit = 'days'
        else:
            # This is "just as wrong" as always returning the currentq
            # date, but prevents unneeded updates each time
            logger.warn('Failed to parse relative date string: %r, falling back to unix epoche', string_)
            return UNIX_EPOCHE

    kwargs = {unit: int(value)}

    # "naive" dates without hours and seconds are created in
    # writers.base_writer.writeStory(), so we don't have to strip
    # hours and minutes from the base date. Using datetime objects
    # would result in a slightly different time (since we calculate
    # the last updated date based on the current time) during each
    # update, since the seconds and hours change.
    today = datetime.utcnow()
    time_ago = timedelta(**kwargs)
    return today - time_ago
