/**
 * Lightbox module for the SSR photos page in lucos_photos.
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
 *       <div class="lightbox-inner" id="lightbox-inner"></div>
 *   </div>
 */
(function () {
    'use strict';

    let lightbox, lightboxInner, lightboxClose, lightboxPrev, lightboxNext;
    let mediaItems = [];
    let currentIndex = -1;

    function showMedia(index) {
        if (index < 0 || index >= mediaItems.length) return;
        currentIndex = index;

        lightboxInner.innerHTML = '';

        const item = mediaItems[index];
        if (item.mediaType === 'video') {
            const video = document.createElement('video');
            video.src = item.originalUrl;
            video.controls = true;
            video.autoplay = true;
            video.preload = 'metadata';
            lightboxInner.appendChild(video);
        } else {
            const img = document.createElement('img');
            img.src = item.originalUrl;
            img.alt = 'Photo ' + (item.id || (index + 1));
            lightboxInner.appendChild(img);
        }

        // Update nav button visibility
        lightboxPrev.style.display = index > 0 ? '' : 'none';
        lightboxNext.style.display = index < mediaItems.length - 1 ? '' : 'none';
    }

    function openLightbox(index) {
        showMedia(index);
        lightbox.classList.add('open');
        document.body.style.overflow = 'hidden';
    }

    function closeLightbox() {
        lightbox.classList.remove('open');
        document.body.style.overflow = '';
        const video = lightboxInner.querySelector('video');
        if (video) video.pause();
        lightboxInner.innerHTML = '';
        currentIndex = -1;
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
     * @param {Array} [items] – optional array of {originalUrl, mediaType, id}.
     *   If omitted, items are collected from .media-card-link[data-original-url]
     *   elements in the DOM.
     */
    function initLightbox(items) {
        lightbox = document.getElementById('lightbox');
        lightboxInner = document.getElementById('lightbox-inner');
        lightboxClose = document.getElementById('lightbox-close');
        lightboxPrev = document.getElementById('lightbox-prev');
        lightboxNext = document.getElementById('lightbox-next');

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

        // Keyboard
        document.addEventListener('keydown', function (e) {
            if (!lightbox.classList.contains('open')) return;
            if (e.key === 'Escape') closeLightbox();
            if (e.key === 'ArrowLeft') prevMedia();
            if (e.key === 'ArrowRight') nextMedia();
        });
    }

    // Export for both module and global usage
    window.initLightbox = initLightbox;
    window.openLightbox = openLightbox;
    window.closeLightbox = closeLightbox;
})();
