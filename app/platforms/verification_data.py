"""What each planned platform's official developer documentation says, checked on 2026-10-04.

One entry per platform key. `has_official_post_api` is None where the official pages could not be opened, so nothing is claimed.
`visibility` of "staff_only" keeps a platform out of member screens (Ops still sees it). `native_formats` overrides single entries
of the platform's own formats where the documentation shows a different way of taking that kind of content.
To change an answer, edit it here, not in the platform files.
"""

VERIFIED_AT = "2026-10-04"

VERDICTS: dict[str, dict] = {'amazon_music': {'has_official_post_api': True,
                  'official_doc_url': 'https://podcasters.amazon.com/submit-rss',
                  'verification_note': 'Official RSS submission page exists; page body was thin when fetched, details '
                                       'from search only. Needs email check and one published episode.'},
 'apple_podcasts': {'has_official_post_api': True,
                    'official_doc_url': 'https://podcasters.apple.com/support/823-podcast-requirements',
                    'verification_note': 'RSS submission via Apple Podcasts Connect, free. Docs describe RSS only, no '
                                         'publish API. Feed must be public with one episode and artwork.'},
 'beehiiv': {'has_official_post_api': True,
             'official_doc_url': 'https://developers.beehiiv.com/api-reference/posts/create',
             'verification_note': 'Create Post endpoint with posts:write scope; docs say available to Max and '
                                  'Enterprise plans only.'},
 'blogger': {'has_official_post_api': True,
             'native_formats': {'image': 'embedded'},
             'official_doc_url': 'https://developers.google.com/blogger/docs/3.0/using',
             'verification_note': 'Blogger API v3 inserts posts with OAuth 2.0. Docs show no image upload; images must '
                                  'be hosted elsewhere.'},
 'buttondown': {'has_official_post_api': True,
                'official_doc_url': 'https://docs.buttondown.com/api-emails-create',
                'verification_note': 'POST /emails with token auth accepts Markdown or HTML. Send behavior and plan '
                                     'limits not confirmed on page.'},
 'buzzsprout': {'has_official_post_api': True,
                'official_doc_url': 'https://github.com/buzzsprout/buzzsprout-api',
                'verification_note': 'Official API repo: create episodes and upload audio in three steps. Plan limits '
                                     'not stated in docs. Docs hosted on Buzzsprout GitHub org.'},
 'circle': {'has_official_post_api': True,
            'official_doc_url': 'https://api.circle.so/apis/admin-api',
            'verification_note': 'Admin API v2 has a Posts API to create published or draft posts in a space; admin '
                                 'API token required. Plan tier and video upload not confirmed.'},
 'dailymotion': {'has_official_post_api': True,
                 'official_doc_url': 'https://developers.dailymotion.com/docs/upload-videos',
                 'verification_note': 'API v2 upload with video.manage token; title, category, visibility, is_for_kids '
                                      'required. Capacity depends on account type.'},
 'deezer': {'has_official_post_api': True,
            'official_doc_url': 'https://creatorsupport.deezer.com/hc/en-us/articles/6328148035741-How-To-Add-Your-Podcast-To-Deezer',
            'verification_note': 'RSS submission at podcasters.deezer.com with email code. Page returned 403 to fetch, '
                                 'details from search. MP3 and FLAC only, M4A not supported.'},
 'devto': {'has_official_post_api': True,
           'native_formats': {'image': 'embedded'},
           'official_doc_url': 'https://developers.forem.com/api/v1',
           'verification_note': 'POST /api/articles with api-key header. No image upload endpoint; main_image takes a '
                                'URL.'},
 'discord': {'has_official_post_api': True,
             'official_doc_url': 'https://docs.discord.com/developers/resources/webhook',
             'verification_note': 'Execute Webhook posts content, embeds and files via multipart/form-data; no OAuth '
                                  'needed, one webhook per channel.'},
 'discourse': {'has_official_post_api': True,
               'official_doc_url': 'https://docs.discourse.org/',
               'verification_note': 'POST /posts.json creates topics and POST /uploads.json uploads images; auth via '
                                    'Api-Key and Api-Username headers, key issued by each site admin.'},
 'farcaster': {'has_official_post_api': True,
               'native_formats': {'image': 'link'},
               'official_doc_url': 'https://github.com/farcasterxyz/protocol/blob/main/docs/SPECIFICATION.md',
               'verification_note': 'Protocol spec: CastAdd messages signed by a user-approved Ed25519 signer, need an '
                                    'FID and storage rent; text max 1024 bytes. Hub HTTP API page not found.'},
 'flickr': {'has_official_post_api': True,
            'official_doc_url': 'https://www.flickr.com/services/api/upload.api.html',
            'verification_note': 'Upload API takes photos via OAuth with write permission; videos are limited by '
                                 'account and may be disabled. Commercial key terms not stated.'},
 'ghost': {'has_official_post_api': True,
           'official_doc_url': 'https://docs.ghost.org/admin-api',
           'verification_note': 'Admin API adds and edits posts and uploads images via integration token. Docs list no '
                                'video or audio upload endpoint.'},
 'github': {'has_official_post_api': True,
            'official_doc_url': 'https://docs.github.com/en/rest/releases/releases#create-a-release',
            'verification_note': 'POST /repos/{owner}/{repo}/releases creates releases with push access; no standalone '
                                 'REST endpoint for Discussions, only linking via a category.'},
 'google_business_profile': {'has_official_post_api': True,
                             'official_doc_url': 'https://developers.google.com/my-business/content/posts-data',
                             'verification_note': 'localPosts create supports event, call to action and offer posts '
                                                  'with photo; OAuth and Google API access approval needed; product '
                                                  'posts unsupported.'},
 'google_chat': {'has_official_post_api': True,
                 'official_doc_url': 'https://developers.google.com/workspace/chat/quickstart/webhooks',
                 'verification_note': 'Incoming webhooks post text and cards to one space; Workspace Business or '
                                      'Enterprise only, admin must allow webhooks, 1 request per second.',
                 'visibility': 'staff_only'},
 'hackernews': {'has_official_post_api': False,
                'official_doc_url': None,
                'verification_note': 'No submission API is documented in the official FAQ. Posting is manual.',
                'visibility': 'staff_only'},
 'hashnode': {'has_official_post_api': True,
              'native_formats': {'image': 'embedded'},
              'official_doc_url': 'https://hashnode.com/announcements/graphql-api',
              'verification_note': 'GraphQL API for publishing; changelog says API access needs a Pro plan. Schema '
                                   'page did not render, mutation not directly seen.'},
 'iheartradio': {'has_official_post_api': True,
                 'official_doc_url': 'https://podcasters.iheart.com/add-podcast/',
                 'verification_note': 'RSS submission at podcasters.iheart.com, free. Submitters in US, Canada, '
                                      'Mexico, Australia, New Zealand only. Page body not readable, details from '
                                      'search.'},
 'imgur': {'has_official_post_api': None,
           'official_doc_url': None,
           'verification_note': 'Official pages could not be opened when checked, so this is not confirmed.'},
 'kakaotalk': {'has_official_post_api': False,
               'official_doc_url': 'https://developers.kakao.com/docs/latest/en/kakaotalk-message/common',
               'verification_note': 'Message API only sends between users of the same service and needs Kakao Talk '
                                    'Social permission, max 5 friends per call; no channel broadcast API.',
               'visibility': 'staff_only'},
 'kit': {'has_official_post_api': True,
         'official_doc_url': 'https://developers.kit.com/api-reference/broadcasts/create-a-broadcast',
         'verification_note': 'v4 POST /broadcasts drafts or schedules sends; API key or OAuth2. Plan limits not '
                              'stated; 403 if insufficient permissions.'},
 'lemmy': {'has_official_post_api': True,
           'official_doc_url': 'https://join-lemmy.org/api/main',
           'verification_note': 'Bearer-auth create post and image upload endpoints exist. Posts need a community_id; '
                                'each instance sets its own rules.'},
 'libsyn': {'has_official_post_api': True,
            'official_doc_url': 'https://api.libsyn.com/',
            'verification_note': 'Libsyn lists a RESTful API for creating and updating content. Episode publish '
                                 'endpoints and access terms not confirmed in pages read.'},
 'line': {'has_official_post_api': True,
          'official_doc_url': 'https://developers.line.biz/en/docs/messaging-api/sending-messages/',
          'verification_note': 'Broadcast message sends text, image, video, audio to all friends of an Official '
                               'Account; monthly message quota applies and extra sends fail.'},
 'mailchimp': {'has_official_post_api': True,
               'official_doc_url': 'https://mailchimp.com/developer/marketing/api/campaigns/add-campaign/',
               'verification_note': 'Marketing API creates and sends campaigns via actions/send. Some actions need '
                                    'paid tiers; plan limits not stated on page.'},
 'mastodon': {'has_official_post_api': True,
              'official_doc_url': 'https://docs.joinmastodon.org/methods/statuses/',
              'verification_note': 'POST /api/v1/statuses with media_ids from a prior media upload; OAuth scope '
                                   'write:statuses. Each server sets its own limits.'},
 'medium': {'has_official_post_api': False,
            'official_doc_url': 'https://github.com/Medium/medium-api-docs',
            'verification_note': 'API archived 2023-03-02, marked unsupported; docs state no new integrations are '
                                 'allowed.',
            'visibility': 'staff_only'},
 'microsoft_teams': {'has_official_post_api': True,
                     'official_doc_url': 'https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook',
                     'verification_note': 'Microsoft 365 Connectors are being retired; use Workflows webhook trigger '
                                          'posting Adaptive Cards, 28 KB limit, 4 requests per second.',
                     'visibility': 'staff_only'},
 'mixcloud': {'has_official_post_api': True,
              'official_doc_url': 'https://www.mixcloud.com/developers/',
              'verification_note': 'POST to api.mixcloud.com/upload with access token. 4 GB audio, 1000 char '
                                   'description, 5 tags. Scheduling needs Pro. Rate limits apply.'},
 'nostr': {'has_official_post_api': True,
           'native_formats': {'image': 'link'},
           'official_doc_url': 'https://github.com/nostr-protocol/nips/blob/master/01.md',
           'verification_note': 'NIP-01: clients publish signed events to relays with ["EVENT", ...]. Open protocol, '
                                'no company approval; relays set their own rules.'},
 'notion': {'has_official_post_api': True,
            'official_doc_url': 'https://developers.notion.com/reference/post-page',
            'verification_note': 'API creates pages and image blocks (URL or file upload) with integration token or '
                                 'OAuth. Workspace content, not a public channel.',
            'visibility': 'staff_only'},
 'patreon': {'has_official_post_api': False,
             'official_doc_url': 'https://docs.patreon.com/',
             'verification_note': 'API v2 post resources are read-only (GET posts by campaign and by id); no endpoint '
                                  'creates posts.',
             'visibility': 'staff_only'},
 'peertube': {'has_official_post_api': True,
              'official_doc_url': 'https://docs.joinpeertube.org/api-rest-reference.html',
              'verification_note': 'OAuth2; resumable video upload via POST /api/v1/videos/upload. Per-instance rules; '
                                   'rate limit 50 calls per 10 seconds.'},
 'pinterest': {'has_official_post_api': None,
               'official_doc_url': None,
               'verification_note': 'Official pages could not be opened when checked, so this is not confirmed.'},
 'pixelfed': {'has_official_post_api': None,
              'official_doc_url': None,
              'verification_note': 'Official pages could not be opened when checked, so this is not confirmed.'},
 'pocket_casts': {'has_official_post_api': True,
                  'official_doc_url': 'https://support.pocketcasts.com/knowledge-base/submitting-podcasts/',
                  'verification_note': 'Anyone can submit an RSS feed URL through the submission form. Feeds marked '
                                       'itunes:block are private. No publish API.'},
 'podbean': {'has_official_post_api': True,
             'official_doc_url': 'https://help.podbean.com/support/solutions/articles/25000008051-publishing-a-new-podcast-episode-via-podbean-api',
             'verification_note': 'OAuth 2.0 API: upload media then publish episode. Developer docs page did not '
                                  'render; plan tiers not stated in the help article.'},
 'podcast_addict': {'has_official_post_api': True,
                    'official_doc_url': 'https://podcastaddict.com/submit',
                    'verification_note': 'Official RSS submission form exists; page returned 403 to fetch, so details '
                                         'come from search. Indexing takes up to 24 hours. No publish API.'},
 'reddit': {'has_official_post_api': None,
            'official_doc_url': None,
            'verification_note': 'Official pages could not be opened when checked, so this is not confirmed.'},
 'resend': {'has_official_post_api': True,
            'native_formats': {'image': 'embedded'},
            'official_doc_url': 'https://resend.com/docs/api-reference/broadcasts/create-broadcast',
            'verification_note': 'Broadcasts API creates and sends or schedules to a segment; needs from, subject, '
                                 'verified domain. No attachments documented.'},
 'shopify_blog': {'has_official_post_api': True,
                  'official_doc_url': 'https://shopify.dev/docs/api/admin-graphql/latest/mutations/articleCreate',
                  'verification_note': 'Admin GraphQL articleCreate needs write_content scope; supports an article '
                                       'image by URL.'},
 'slack': {'has_official_post_api': True,
           'official_doc_url': 'https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks',
           'verification_note': 'Incoming webhooks post JSON text and blocks to one channel; file upload is not part '
                                'of webhooks, needs a bot token.',
           'visibility': 'staff_only'},
 'snapchat': {'has_official_post_api': False,
              'official_doc_url': 'https://developers.snap.com/snap-kit/creative-kit/overview',
              'verification_note': 'Creative Kit only hands content to the Snapchat app; the user edits and sends it. '
                                   'No documented API to publish to Stories or Spotlight.',
              'visibility': 'staff_only'},
 'soundcloud': {'has_official_post_api': True,
                'official_doc_url': 'https://developers.soundcloud.com/docs/api/guide',
                'verification_note': 'API uploads tracks via OAuth user consent, 4 GB and 24 hour limit. Registering '
                                     'an app needs an Artist Pro account. Not a podcast host.'},
 'spotify': {'has_official_post_api': True,
             'official_doc_url': 'https://support.spotify.com/us/creators/article/claiming-your-podcast-on-spotify-for-creators/',
             'verification_note': 'RSS submission via Spotify for Creators with email code check. A show can be '
                                  'claimed once only. No publish API; video not confirmed in docs read.'},
 'substack': {'has_official_post_api': False,
              'official_doc_url': 'https://substack.com/api-tos',
              'verification_note': 'Official developer API only returns public creator profile data under approval; no '
                                   'publishing endpoint.',
              'visibility': 'staff_only'},
 'telegram': {'has_official_post_api': True,
              'official_doc_url': 'https://core.telegram.org/bots/api',
              'verification_note': 'Bot API sendMessage, sendPhoto, sendVideo, sendAudio post to channels where the '
                                   'bot is admin; free, no approval.'},
 'tiktok': {'has_official_post_api': None,
            'official_doc_url': None,
            'verification_note': 'Official pages could not be opened when checked, so this is not confirmed.'},
 'transistor': {'has_official_post_api': True,
                'official_doc_url': 'https://developers.transistor.fm/',
                'verification_note': 'API creates, uploads (5 GB) and publishes or schedules episodes. Video upload '
                                     'needs a video-enabled plan.'},
 'tumblr': {'has_official_post_api': True,
            'official_doc_url': 'https://www.tumblr.com/docs/en/api/v2',
            'verification_note': 'OAuth; POST /posts (NPF) creates posts. Per user per day: 250 posts, 250 images, 20 '
                                 'videos, 60 minutes of video.'},
 'tunein': {'has_official_post_api': True,
            'official_doc_url': 'https://help.tunein.com/en/support/solutions/articles/151000172121-how-do-i-add-my-podcast-to-tunein-',
            'verification_note': 'Self-service request form through the TuneIn broadcaster portal. Manual review, no '
                                 'publish API. Article gives no RSS spec or timing.'},
 'vimeo': {'has_official_post_api': True,
           'official_doc_url': 'https://help.vimeo.com/hc/en-us/articles/22366164249105-How-to-upload-videos-by-using-the-Vimeo-API',
           'verification_note': 'Upload via API using resumable (tus), form POST or pull. Scopes and upload-access '
                                'request not confirmed from the opened page; plan limits apply.'},
 'vk': {'has_official_post_api': None,
        'official_doc_url': None,
        'verification_note': 'Official pages could not be opened when checked, so this is not confirmed.'},
 'webflow': {'has_official_post_api': True,
             'official_doc_url': 'https://developers.webflow.com/data/reference/cms/collection-items/staged-items/create-item',
             'verification_note': 'Data API v2 creates CMS items with CMS:write scope and bearer token. Plan '
                                  'requirements not stated in the docs.'},
 'wechat': {'has_official_post_api': True,
            'official_doc_url': 'https://developers.weixin.qq.com/doc/subscription/en/api/public/api_freepublish_submit',
            'verification_note': 'freepublish/submit publishes a saved draft article; only authenticated (verified) '
                                 'Official or Service Accounts of an enterprise entity may call it.'},
 'weibo': {'has_official_post_api': None,
           'official_doc_url': None,
           'verification_note': 'Official pages could not be opened when checked, so this is not confirmed.'},
 'whatsapp_business': {'has_official_post_api': True,
                       'official_doc_url': 'https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages',
                       'verification_note': 'Cloud API sends text, image, video, audio to opted-in users; outside the '
                                            '24 hour window only approved templates are allowed, paid per message.'},
 'whatsapp_channels': {'has_official_post_api': False,
                       'official_doc_url': 'https://developers.facebook.com/docs/whatsapp/',
                       'verification_note': 'Meta WhatsApp developer docs list business messaging, groups, catalogs '
                                            'and webhooks; no Channels publishing API is documented.',
                       'visibility': 'staff_only'},
 'wordpress': {'has_official_post_api': True,
               'official_doc_url': 'https://developer.wordpress.org/rest-api/reference/posts/',
               'verification_note': 'REST API POST /wp/v2/posts; Application Passwords (WP 5.6+) over HTTPS auth. Site '
                                    'owner must enable REST and HTTPS.'},
 'wordpress_com': {'has_official_post_api': True,
                   'official_doc_url': 'https://developer.wordpress.com/docs/api/',
                   'verification_note': 'WordPress.com REST API with OAuth2 for posts. Plan restrictions not stated in '
                                        'the docs page; confirm per site plan.'},
 'youtube_music': {'has_official_post_api': True,
                   'official_doc_url': 'https://support.google.com/youtubemusic/answer/13525207?hl=en',
                   'verification_note': 'RSS ingestion in YouTube Studio in select countries. Makes static-image '
                                        'videos on the channel. No ads allowed; audio cannot be updated after '
                                        'publish.'}}
