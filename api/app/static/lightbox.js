/**
 * Lightbox module for lucos_photos.
 *
 * Usage:
 *   1. Include the lightbox HTML in your page (see lightbox markup below).
 *   2. Call initLightbox() after the DOM is ready.
 *   3. Add data-original-url and data-media-type attributes to each
 *      .media-card-link element — the lightbox auto-binds to them.
 *
 * Lightbox HTML (add inside your template):
 *   <div class="lightbox" id="lightbox" role="dialog" aria-modal="true" aria-label="Media viewer">
 *       <button class="lightbox-close" id="lightbox-close" aria-label="Close">&times;</button>
 *       <button class="lightbox-nav lightbox-prev" id="lightbox-prev" aria-label="Previous">&lsaquo;</button>
 *       <button class="lightbox-nav lightbox-next" id="lightbox-next" aria-label="Next">&rsaquo;</button>
 *       <div class="lightbox-content">
 *           <div class="lightbox-inner" id="lightbox-inner"></div>
 *           <div class="lightbox-metadata" id="lightbox-metadata"></div>
 *       </div>
 *   </div>
 */
(function () {
    'use strict';

    var lightbox, lightboxInner, lightboxClose, lightboxPrev, lightboxNext, lightboxMetadata;
    var mediaItems = [];
    var currentIndex = -1;
    var triggerElement = null;
    var metadataCache = {};
    var currentFetchId = 0;

    function formatDate(isoString) {
        if (!isoString) return null;
        try {
            var d = new Date(isoString);
            return d.toLocaleDateString(undefined, {
                year: 'numeric', month: 'long', day: 'numeric',
                hour: '2-digit', minute: '2-digit'
            });
        } catch (e) {
            return isoString;
        }
    }

    function renderMetadata(data) {
        var html = '<dl class="lightbox-metadata-list">';

        if (data.takenAt) {
            html += '<dt>Taken</dt><dd>' + formatDate(data.takenAt) + '</dd>';
        }

        if (data.width && data.height) {
            html += '<dt>Dimensions</dt><dd>' + data.width + ' &times; ' + data.height + '</dd>';
        }

        if (data.fileExtension) {
            html += '<dt>Format</dt><dd>' + data.fileExtension.toUpperCase() + '</dd>';
        }

        if (data.people && data.people.length > 0) {
            html += '<dt>People</dt><dd class="lightbox-people">';
            data.people.forEach(function (person) {
                html += '<a href="/people/' + person.id + '" class="lightbox-person-link">';
                if (person.profilePictureUrl) {
                    html += '<img class="lightbox-person-pic" src="' + person.profilePictureUrl + '" alt="">';
                }
                html += '<span>' + (person.name || 'Unknown') + '</span>';
                html += '</a>';
            });
            html += '</dd>';
        }

        html += '</dl>';
        html += '<a href="/photos/' + data.id + '" class="lightbox-details-link">View full details</a>';
        return html;
    }

    function fetchAndShowMetadata(photoId) {
        if (!lightboxMetadata || !photoId) {
            if (lightboxMetadata) lightboxMetadata.innerHTML = '';
            return;
        }

        // If cached, render immediately
        if (metadataCache[photoId]) {
            lightboxMetadata.innerHTML = renderMetadata(metadataCache[photoId]);
            return;
        }

        lightboxMetadata.innerHTML = '<p class="lightbox-metadata-loading">Loading details&hellip;</p>';

        var fetchId = ++currentFetchId;
        fetch('/photos/' + photoId, { headers: { 'Accept': 'application/json' } })
            .then(function (res) {
                if (!res.ok) throw new Error('Failed to load');
                return res.json();
            })
            .then(function (data) {
                metadataCache[photoId] = data;
                // Only render if this is still the current photo
                if (fetchId === currentFetchId) {
                    lightboxMetadata.innerHTML = renderMetadata(data);
                }
            })
            .catch(function () {
                if (fetchId === currentFetchId) {
                    lightboxMetadata.innerHTML = '';
                }
            });
    }

    function showMedia(index) {
        if (index < 0 || index >= mediaItems.length) return;
        currentIndex = index;

        lightboxInner.innerHTML = '';

        var item = mediaItems[index];
        if (item.mediaType === 'video') {
            var video = document.createElement('video');
            video.src = item.originalUrl;
            video.controls = true;
            video.autoplay = true;
            video.preload = 'metadata';
            lightboxInner.appendChild(video);
        } else {
            var img = document.createElement('img');
            img.src = item.originalUrl;
            img.alt = 'Photo';
            lightboxInner.appendChild(img);
        }

        // Update nav button visibility
        lightboxPrev.style.display = index > 0 ? '' : 'none';
        lightboxNext.style.display = index < mediaItems.length - 1 ? '' : 'none';

        // Fetch and display metadata
        fetchAndShowMetadata(item.id);
    }

    function openLightbox(index) {
        triggerElement = document.activeElement;
        showMedia(index);
        lightbox.classList.add('open');
        document.body.style.overflow = 'hidden';
        lightboxClose.focus();
    }

    function closeLightbox() {
        lightbox.classList.remove('open');
        document.body.style.overflow = '';
        var video = lightboxInner.querySelector('video');
        if (video) video.pause();
        lightboxInner.innerHTML = '';
        if (lightboxMetadata) lightboxMetadata.innerHTML = '';
        currentIndex = -1;
        // Restore focus to the element that opened the lightbox
        if (triggerElement) {
            triggerElement.focus();
            triggerElement = null;
        }
    }

    function prevMedia() {
        if (currentIndex > 0) showMedia(currentIndex - 1);
    }

    function nextMedia() {
        if (currentIndex < mediaItems.length - 1) showMedia(currentIndex + 1);
    }

    /**
     * Initialise the lightbox.
     *
     * @param {Array} [items] - optional array of {originalUrl, mediaType, id}.
     *   If omitted, items are collected from .media-card-link[data-original-url]
     *   elements in the DOM.
     */
    function initLightbox(items) {
        lightbox = document.getElementById('lightbox');
        lightboxInner = document.getElementById('lightbox-inner');
        lightboxClose = document.getElementById('lightbox-close');
        lightboxPrev = document.getElementById('lightbox-prev');
        lightboxNext = document.getElementById('lightbox-next');
        lightboxMetadata = document.getElementById('lightbox-metadata');

        if (!lightbox) return;

        // Build the media list
        if (items) {
            mediaItems = items;
        } else {
            // Collect from DOM data attributes
            mediaItems = [];
            document.querySelectorAll('.media-card-link[data-original-url]').forEach(function (link) {
                mediaItems.push({
                    originalUrl: link.getAttribute('data-original-url'),
                    mediaType: link.getAttribute('data-media-type') || 'image',
                    id: link.getAttribute('data-photo-id') || '',
                });
            });
        }

        // Bind click handlers on .media-card-link elements
        document.querySelectorAll('.media-card-link[data-original-url]').forEach(function (link, idx) {
            link.addEventListener('click', function (e) {
                if (e.button === 0 && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
                    e.preventDefault();
                    openLightbox(idx);
                }
            });
        });

        // Close handlers
        lightboxClose.addEventListener('click', closeLightbox);
        lightbox.addEventListener('click', function (e) {
            if (e.target === lightbox) closeLightbox();
        });

        // Nav handlers
        lightboxPrev.addEventListener('click', function (e) {
            e.stopPropagation();
            prevMedia();
        });
        lightboxNext.addEventListener('click', function (e) {
            e.stopPropagation();
            nextMedia();
        });

        // Keyboard navigation and focus trapping
        document.addEventListener('keydown', function (e) {
            if (!lightbox.classList.contains('open')) return;
            if (e.key === 'Escape') closeLightbox();
            if (e.key === 'ArrowLeft') prevMedia();
            if (e.key === 'ArrowRight') nextMedia();

            // Focus trapping — keep Tab within the lightbox
            if (e.key === 'Tab') {
                var focusable = lightbox.querySelectorAll('button:not([style*="display: none"]), a[href]');
                if (focusable.length === 0) return;
                var first = focusable[0];
                var last = focusable[focusable.length - 1];
                if (e.shiftKey) {
                    if (document.activeElement === first) {
                        e.preventDefault();
                        last.focus();
                    }
                } else {
                    if (document.activeElement === last) {
                        e.preventDefault();
                        first.focus();
                    }
                }
            }
        });
    }

    // Export for both module and global usage
    window.initLightbox = initLightbox;
    window.openLightbox = openLightbox;
    window.closeLightbox = closeLightbox;
})();
