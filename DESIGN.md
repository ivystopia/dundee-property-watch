# Design selection

Compared Just the Docs, Pico CSS and Tabler on 26 September 2026 using the same three real Dundee property cards, including photos, price qualifiers, dates, agent/portal links and a longer fee note. Desktop screenshots used a 1440 px viewport; the initial narrow comparison used Firefox's effective 500 px viewport. The selected report was also checked in an actual 390 px iframe viewport, with no horizontal overflow.

The reference repository, ivystopia.github.io, uses Just the Docs v0.10.1 with a light colour scheme, no sidebar and no search. Its public homepage returned 404 during this review, so the comparison used its local configuration and the current official Just the Docs stylesheet. This is a comparison of its design approach, not a claim to have captured the currently deployed private repository.

| Candidate | Screenshot observations | Fit for this report |
| --- | --- | --- |
| Just the Docs | Clean typography and unobtrusive separators; familiar light appearance. Its documentation layout leaves property prices, pictures and notes less clearly grouped. | A useful reference for simplicity; better suited to long text documents. |
| Pico CSS 2.1.1, Jade | Strong price/address hierarchy and clear white cards. Pictures stack naturally at narrow widths and the links remain readable. | Selected, with smaller shadows and compact spacing for the growing archive. |
| Tabler Core 1.6.0 | Clear card boundaries and compact layout; a denser dashboard appearance. | Capable, but its much larger dashboard framework is unnecessary for a read-only report. |

The report keeps the reference site's light appearance and adds Pico's card styling. All stylesheets are served with the site, using a pinned, unmodified Pico release and its MIT licence. There is no external font, theme CDN, JavaScript framework or Jekyll dependency at runtime. Plain HTML pages preserve working archive navigation even without JavaScript. Property pictures remain hotlinked to their original sources, as before.

Primary references:

- https://just-the-docs.com/
- https://github.com/just-the-docs/just-the-docs/releases/tag/v0.10.1
- https://picocss.com/examples
- https://picocss.com/docs/card
- https://picocss.com/docs/version-picker
- https://github.com/picocss/pico/releases/tag/v2.1.1
- https://preview.tabler.io/cards.html
- https://github.com/tabler/tabler/releases/tag/%40tabler/core%401.6.0
- https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site

The local migration evidence includes all six comparison screenshots, the final desktop/mobile screenshots and an automated before/after comparison of every archived card's visible text, links and image URL. It is kept outside the public repository with the private migration snapshot.
