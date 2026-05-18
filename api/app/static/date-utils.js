/**
 * Date formatting utilities for lucos_photos.
 *
 * Exposes window.formatHumanDate(isoString) — formats an ISO datetime as
 * "Weds 29th April 2026 at 6:31pm" using informal British English day
 * abbreviations (Mon, Tues, Weds, Thurs, Fri, Sat, Sun).
 */
(function () {
    'use strict';

    var DAY_ABBR = ['Sun', 'Mon', 'Tues', 'Weds', 'Thurs', 'Fri', 'Sat'];
    var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
        'July', 'August', 'September', 'October', 'November', 'December'];

    function ordinal(n) {
        var mod100 = n % 100, mod10 = n % 10;
        if (mod100 >= 11 && mod100 <= 13) return n + 'th';
        if (mod10 === 1) return n + 'st';
        if (mod10 === 2) return n + 'nd';
        if (mod10 === 3) return n + 'rd';
        return n + 'th';
    }

    window.formatHumanDate = function formatHumanDate(isoString) {
        if (!isoString) return null;
        try {
            var d = new Date(isoString);
            var hour12 = d.getHours() % 12 || 12;
            var mins = String(d.getMinutes()).padStart(2, '0');
            var ampm = d.getHours() >= 12 ? 'pm' : 'am';
            return DAY_ABBR[d.getDay()] + ' ' + ordinal(d.getDate()) + ' ' +
                MONTH_NAMES[d.getMonth()] + ' ' + d.getFullYear() +
                ' at ' + hour12 + ':' + mins + ampm;
        } catch (e) {
            return isoString;
        }
    };
})();
