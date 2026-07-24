/**
 * Shared "no profile picture" placeholder fallback for lucos_photos.
 *
 * Exposes window.lucosProfilePicturePlaceholderFallback(img) — wired to a
 * profile-picture <img>'s onerror handler so a failed load (404, stale
 * derivative reference, network error) degrades to the same static
 * placeholder treatment used when no picture exists at all, instead of the
 * browser's default broken-image glyph.
 *
 * Reads sizing/labelling from data-pfp-extra-class / data-pfp-aria-label
 * attributes set on the <img> in the template. The markup built here must
 * stay visually identical to templates/_profile_picture_placeholder.html,
 * which renders the equivalent placeholder server-side for the no-picture
 * case.
 */
(function () {
    'use strict';

    var ICON_PATH = 'M12 2.25c-2.9 0-5.25 2.35-5.25 5.25S9.1 12.75 12 12.75s5.25-2.35 5.25-5.25S14.9 2.25 12 2.25zM12 14.25c-4.14 0-9.75 2.08-9.75 6.19v1.31h19.5v-1.31c0-4.11-5.61-6.19-9.75-6.19z';

    window.lucosProfilePicturePlaceholderFallback = function (img) {
        var div = document.createElement('div');
        div.className = 'person-profile-picture-placeholder';
        if (img.dataset.pfpExtraClass) {
            div.classList.add(img.dataset.pfpExtraClass);
        }
        if (img.dataset.pfpAriaLabel) {
            div.setAttribute('role', 'img');
            div.setAttribute('aria-label', img.dataset.pfpAriaLabel);
        }
        div.innerHTML = '<svg class="profile-picture-placeholder-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="' + ICON_PATH + '"></path></svg>';
        img.replaceWith(div);
    };
})();
