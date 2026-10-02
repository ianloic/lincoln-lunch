# lincoln-lunch

The Lincoln Middle School (Alameda, CA) lunch menu as a subscribable calendar,
rebuilt nightly and published on GitHub Pages:

- Page: https://ianloic.github.io/lincoln-lunch/
- Calendar feed: https://ianloic.github.io/lincoln-lunch/lincoln-lunch.ics
  (`webcal://ianloic.github.io/lincoln-lunch/lincoln-lunch.ics` to subscribe)

Each school day is an all-day event whose title lists the entrées and whose
description has the full menu (sides, salad bar item).

## How it works

AUSD publishes menus through School Nutrition and Fitness. Its menu pages are
a JavaScript app backed by a public GraphQL API
(`api.schoolnutritionandfitness.com/graphql`) that returns structured items:
each has a day of the month, a product name and a category. `lincoln_lunch.py`:

1. follows the district's "Lincoln Middle School Lunch Menu" link
   (`downloadMenu.php/1571761233982/904073`), which redirects to the current
   month's menu id;
2. fetches that month, then walks the API's previous/next-month links (up to
   12 back and 6 forward);
3. groups each day's items into dishes and writes `site/lincoln-lunch.ics`
   and `site/index.html`.

It uses only the Python standard library.

```sh
python3 -m unittest        # tests
python3 lincoln_lunch.py   # writes ./site
```

`.github/workflows/publish.yml` runs the tests, builds, and deploys to Pages
nightly, on pushes to `main`, and on demand. If a build fails (site down, API
change, empty menu), nothing is deployed and the previous calendar stays up;
GitHub emails you about the failed run.

## Setup

In the repository settings, under **Pages → Build and deployment → Source**,
choose **GitHub Actions**. Then run the workflow from the Actions tab (or push
to `main`).
