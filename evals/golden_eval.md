# Golden-50 evaluation

Labeled by a person: 50. Predictions available: 50.

| Field | Agreement (incl. human-listed alternatives) | Strict (primary label only) |
|---|---|---|
| topic | 0.54 | 0.54 |
| intent | 0.9 | 0.9 |
| severity exact | 0.72 | 0.72 |

Severity MAE 0.5; sentiment within ±0.3: 0.76 (MAE 0.254); quotes exact substrings: 1.0; cases with entities not in text: 14.

Post-hoc adjudication (labeler, after seeing predictions): {'human_error': 16, 'both_ok': 8, 'model_error': 4}; all-three-fields acceptable 0.92 vs blind 0.44.

## Disagreements

| Review | Human (topic/intent/sev) | Model | Adjudication | Text |
|---|---|---|---|---|
| `292ce26a` | access/complaint/5 | other/complaint/2 | human_error | Staff must be full of mentally ill kool-aid heads. Banning conservative music that speaks  |
| `a70e8d3e` | support/praise/3 | other/praise/1 | human_error | Great app use it every chance I get |
| `0d4f8b48` | other/praise/3 | other/praise/1 | human_error | It's really an amazing experience with Spotify...I love it🥰 |
| `b5ee7834` | catalog/praise/1 | other/praise/1 | human_error | Spotify is awesome and and very diverse :) |
| `1fc8b08f` | access/praise/1 | usability/praise/1 | both_ok | Duo Premium .... No ads at all! |
| `947821dc` | support/complaint/4 | playback/complaint/3 | human_error | Very good but wen I play my song it o ly plays a little bit then it stops I updated my pho |
| `723f07de` | usability/request/4 | playback/complaint/4 | both_ok | Update: I reached out to Spotify Support and with some help from Sarah I was able to get t |
| `47428620` | usability/complaint/3 | other/complaint/2 | human_error | This app is bad work |
| `ac6cd66d` | other/unclear/1 | catalog/unclear/1 | both_ok | Lhat ng song nasa spotify |
| `46842184` | other/unclear/1 | catalog/complaint/2 | model_error | asem, lirik nya kdang gaada kek mana?? |
| `5de7f95b` | other/cancellation/5 | other/unclear/1 | model_error | We are boycotting Swedish app in our protest against Sweden for their desrecpect & descera |
| `372d4e67` | other/unclear/1 | usability/request/2 | both_ok | Please ye ads ko thoda kumm Karo harr ek song ke baad ad 🙏🙄 |
| `3ddb3f4e` | usability/cancellation/4 | billing/cancellation/3 | human_error | Deleting this App. The new update suck, we can't choose our fvrt songs after some clicks,  |
| `16d640e9` | billing/complaint/2 | billing/complaint/3 | both_ok | It's my best music app but now it's not...everything basic features is premium.... |
| `8cc4fad4` | other/complaint/5 | other/complaint/1 | model_error | I hate this app and sweden .I love islam |
| `215463ae` | usability/complaint/4 | playback/complaint/4 | both_ok | What is happening with Spotify?? Recently I'm trying to play a song but it doesn't working |
| `46c0b49f` | usability/complaint/3 | playback/complaint/4 | human_error | Are you out of your mind? This is dangerous! Listening to music in a normal volume and sud |
| `283b5843` | other/unclear/1 | billing/unclear/1 | both_ok | Beth, we all got to give me a free dollar to go with 3 months, bro I can't be like |
| `2aa566c6` | other/praise/1 | usability/praise/1 | human_error | Nice app, very less advertising |
| `8c0546b0` | playback/complaint/3 | catalog/complaint/3 | human_error | 1-Lyrics not getting loaded, 2-can't go back to or start song where to wanted. Pathetic up |
| `b58dd6f0` | catalog/complaint/2 | usability/request/1 | human_error | The only thing Spotify lacks is the ability to organize saved  albums/artists into collect |
| `1a41c7e1` | usability/complaint/2 | other/complaint/2 | human_error | Worst application 👎👎👎 |
| `dceb14e7` | downloads/complaint/3 | playback/complaint/3 | human_error | I gave one star because I can't give any less than that. the new update is sooooooo annoyi |
| `5fba12ea` | other/praise/1 | catalog/praise/1 | both_ok | I find Spotify very good, it finds and play whatever music you want and also adds songs th |
| `ac4e860a` | playback/complaint/2 | playback/complaint/4 | model_error | paying for subscribing but unable to play in offline mode really? |
| `9034d675` | other/complaint/2 | usability/complaint/2 | human_error | Worst app ever too much ad |
| `d2f3874f` | usability/complaint/3 | billing/complaint/3 | human_error | Too expensive and the free version is pretty much unusable with constant ads... Every two  |
| `99b93e31` | usability/complaint/2 | other/complaint/2 | human_error | This update very bad for everyone.old Spotify. we all want. |
