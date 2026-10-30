"""Price changes create new report entries without creating new identities."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest

from render import render
import test_watch


class PriceChangeTests(unittest.TestCase):
    setUp = test_watch.WatchTests.setUp
    save_evidence = test_watch.WatchTests.save_evidence
    tearDown = test_watch.WatchTests.tearDown
    result = test_watch.WatchTests.result
    ingest = test_watch.WatchTests.ingest

    def next_day(self):
        self.started = (datetime.fromisoformat(self.started) + timedelta(days=1)).isoformat()
        self.day = self.started[:10]

    def at_price(self, price):
        p = deepcopy(self.p)
        p['price_gbp'] = price
        for item in p['evidence']:
            if item['field'] == 'price':
                item['quote'] = f'Offers over £{price:,}'
        (self.job / 'evidence/property.json').write_text(json.dumps({
            'url': p['url'], 'status': 200, 'error': '', 'retrieved_at': self.started,
            'text': '. '.join(item['quote'] for item in p['evidence'])}))
        return p

    def reports(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM reports ORDER BY day')]

    def test_reduction_increase_and_return_to_previous_price(self):
        self.assertEqual(self.ingest('first', self.result(self.at_price(260000)))['new'], 1)
        first_seen = self.db.execute('SELECT first_seen FROM properties').fetchone()[0]
        for index, price in enumerate([250000, 255000, 260000]):
            self.next_day()
            result = self.ingest(str(index), self.result(self.at_price(price)))
            self.assertEqual((result['new'], result['price_changes'], result['duplicates'], result['rejected']), (0, 1, 0, 0))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM properties').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT first_seen FROM properties').fetchone()[0], first_seen)
        reports = self.reports()
        self.assertEqual([p['price_gbp'] for p in reports], [260000, 250000, 255000, 260000])
        self.assertEqual([p['previous_price_gbp'] for p in reports[1:]], [260000, 250000, 255000])
        self.assertNotIn('previous_price_gbp', reports[0])
        html = render(self.db, self.root / 'site').read_text()
        self.assertIn('Previously reported · Price changed from £255,000 to £260,000', html)
        self.assertIn('0 new · 1 price change', html)
        self.assertEqual(html.count('<article'), 4)

    def test_unchanged_price_and_same_day_repeats_do_not_reannounce(self):
        self.ingest('first', self.result(self.at_price(260000)))
        self.next_day()
        self.assertEqual(self.ingest('unchanged', self.result(self.at_price(260000)))['new'], 0)
        self.assertEqual(self.ingest('changed', self.result(self.at_price(250000)))['price_changes'], 1)
        self.assertEqual(self.ingest('repeat', self.result(self.at_price(250000)))['new'], 0)
        self.assertEqual(self.ingest('conflict', self.result(self.at_price(260000)))['new'], 0)
        self.assertEqual([p['price_gbp'] for p in self.reports()], [260000, 250000])
        self.next_day()
        self.assertEqual(self.ingest('next-unchanged', self.result(self.at_price(250000)))['new'], 0)

    def test_cap_and_current_evidence_still_required(self):
        self.ingest('first', self.result(self.at_price(260000)))
        self.next_day()
        self.assertEqual(self.ingest('over-cap', self.result(self.at_price(270001)))['rejected'], 1)
        p = self.at_price(250000)
        p['evidence'][1]['quote'] = 'Offers over £240,000'
        self.assertEqual(self.ingest('bad-proof', self.result(p))['rejected'], 1)
        self.assertEqual(len(self.reports()), 1)

    def test_legacy_price_change_is_reported_once(self):
        self.db.execute('INSERT INTO legacy(id,data) VALUES (?,?)',
                        ('legacy', json.dumps({'address': self.p['address'], 'price_gbp': 260000})))
        self.db.commit()
        p = self.at_price(250000)
        p.update(prior_report_id='legacy', identity_note='Exact address and unit match')
        self.assertEqual(self.ingest('legacy-change', self.result(p))['price_changes'], 1)
        self.assertEqual(self.reports()[0]['previous_price_gbp'], 260000)
        self.next_day()
        p = self.at_price(250000)
        p.update(prior_report_id='legacy', identity_note='Exact address and unit match')
        self.assertEqual(self.ingest('legacy-repeat', self.result(p))['new'], 0)


if __name__ == '__main__':
    unittest.main()
