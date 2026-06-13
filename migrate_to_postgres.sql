-- Migration of Empire Earth MMR data from SQLite (empire.db) into Neon Postgres.
-- Safe to re-run: it clears and reloads players + matches.
BEGIN;
TRUNCATE players, matches RESTART IDENTITY;

INSERT INTO players (id, name, mmr, wins, losses, mu, sigma, created_at) VALUES
  (1, '3amar404', 1431, 23, 22, 25.694225535892617, 4.977149048347818, '2026-04-03T21:39:23.809003'),
  (2, 'Tuni5', 1501, 17, 12, 29.43714533221116, 5.6405993367791645, '2026-04-03T21:39:24.036070'),
  (3, 'zled', 1055, 12, 21, 17.773212767431403, 5.467740212818797, '2026-04-03T21:39:24.260348'),
  (4, 'momo', 1184, 18, 23, 20.34086541406121, 5.246281098857619, '2026-04-03T21:39:24.486786'),
  (5, 'barabi7', 1680, 21, 14, 33.87439400772271, 5.622800709290281, '2026-04-03T21:39:24.719619'),
  (6, 'old_kala5la5', 1298, 6, 5, 29.22624683483809, 7.2564397170665975, '2026-04-03T21:39:24.982657'),
  (7, 'SPoUD', 965, 7, 10, 18.775695503130848, 6.549760777239158, '2026-04-03T21:39:25.237226'),
  (8, 'De3ech', 1403, 13, 9, 28.618547619865875, 6.184561970264373, '2026-04-03T21:39:25.479371'),
  (9, 'Farouk', 516, 2, 7, 9.236170475499812, 7.111947560449455, '2026-04-30T19:00:00'),
  (10, 'Dex55', 912, 2, 2, 21.305200948129066, 7.8326411495543455, '2026-05-09 21:21:04');

INSERT INTO matches (id, team1, team2, winner, mmr_changes, status, created_at) VALUES
  (1, '["3amar404", "SPoUD"]', '["momo", "zled"]', 'team1', '{"3amar404": "+97", "SPoUD": "+97", "momo": "-60", "zled": "-60"}', 'approved', '2026-04-03T22:59:52.376443'),
  (2, '["3amar404", "momo", "zled"]', '["barabi7", "De3ech", "SPoUD"]', 'team2', '{"barabi7": "+73", "De3ech": "+73", "SPoUD": "+71", "3amar404": "-45", "momo": "-45", "zled": "-45"}', 'approved', '2026-04-06T21:59:09.774412'),
  (3, '["3amar404", "momo"]', '["barabi7", "SPoUD"]', 'team2', '{"barabi7": "+79", "SPoUD": "+75", "3amar404": "-42", "momo": "-42"}', 'approved', '2026-04-07T18:27:57.399468'),
  (4, '["3amar404", "SPoUD"]', '["barabi7", "momo"]', 'team2', '{"barabi7": "+108", "momo": "+103", "3amar404": "-70", "SPoUD": "-70"}', 'approved', '2026-04-07T18:46:24.393492'),
  (5, '["3amar404", "barabi7"]', '["momo", "SPoUD"]', 'team1', '{"3amar404": "+85", "barabi7": "+88", "momo": "-51", "SPoUD": "-51"}', 'approved', '2026-04-07T19:33:56.163935'),
  (6, '["3amar404", "old_kala5la5", "zled"]', '["barabi7", "momo", "SPoUD"]', 'team1', '{"3amar404": "+81", "old_kala5la5": "+97", "zled": "+91", "barabi7": "-62", "momo": "-60", "SPoUD": "-59"}', 'approved', '2026-04-08T22:18:20.895081'),
  (7, '["3amar404", "old_kala5la5", "zled"]', '["barabi7", "momo", "SPoUD"]', 'team2', '{"barabi7": "+75", "momo": "+73", "SPoUD": "+72", "3amar404": "-50", "old_kala5la5": "-57", "zled": "-55"}', 'approved', '2026-04-08T23:09:42.261543'),
  (8, '["3amar404", "De3ech", "momo"]', '["barabi7", "Tuni5"]', 'team2', '{"barabi7": "+95", "Tuni5": "+116", "3amar404": "-70", "De3ech": "-84", "momo": "-71"}', 'approved', '2026-04-11T22:39:07.732423'),
  (9, '["3amar404", "De3ech", "momo", "zled"]', '["barabi7", "old_kala5la5", "Tuni5"]', 'team1', '{"3amar404": "+61", "De3ech": "+75", "momo": "+61", "zled": "+70", "barabi7": "-45", "old_kala5la5": "-52", "Tuni5": "-53"}', 'approved', '2026-04-11T23:44:53.219638'),
  (10, '["3amar404", "De3ech", "Tuni5"]', '["barabi7", "momo", "SPoUD"]', 'team1', '{"3amar404": "+69", "De3ech": "+85", "Tuni5": "+88", "barabi7": "-51", "momo": "-49", "SPoUD": "-52"}', 'approved', '2026-04-12T13:10:03.934266'),
  (11, '["3amar404", "De3ech", "momo", "SPoUD"]', '["barabi7", "old_kala5la5", "Tuni5"]', 'team1', '{"3amar404": "+45", "De3ech": "+54", "momo": "+44", "SPoUD": "+47", "barabi7": "-29", "old_kala5la5": "-34", "Tuni5": "-34"}', 'approved', '2026-04-12T14:47:05.379338'),
  (12, '["3amar404", "barabi7", "old_kala5la5", "zled"]', '["De3ech", "momo", "SPoUD", "Tuni5"]', 'team1', '{"3amar404": "+51", "barabi7": "+53", "old_kala5la5": "+65", "zled": "+62", "De3ech": "-42", "momo": "-36", "SPoUD": "-38", "Tuni5": "-44"}', 'approved', '2026-04-12T21:46:39.449063'),
  (13, '["3amar404", "zled"]', '["De3ech", "Tuni5"]', 'team2', '{"De3ech": "+87", "Tuni5": "+91", "3amar404": "-46", "zled": "-54"}', 'approved', '2026-04-15T22:07:06.891656'),
  (14, '["3amar404", "Tuni5"]', '["De3ech", "zled"]', 'team1', '{"3amar404": "+73", "Tuni5": "+90", "De3ech": "-53", "zled": "-53"}', 'approved', '2026-04-15T22:59:45.566438'),
  (17, '["3amar404", "barabi7", "old_kala5la5"]', '["De3ech", "momo", "SPoUD", "zled"]', 'team1', '{"3amar404": "+63", "barabi7": "+71", "old_kala5la5": "+83", "De3ech": "-55", "momo": "-51", "SPoUD": "-53", "zled": "-55"}', 'approved', '2026-04-16T22:19:13.738437'),
  (18, '["3amar404", "Tuni5", "zled"]', '["barabi7", "De3ech", "momo"]', 'team2', '{"barabi7": "+68", "De3ech": "+71", "momo": "+65", "3amar404": "-43", "Tuni5": "-51", "zled": "-49"}', 'approved', '2026-04-17T22:32:50.365107'),
  (19, '["3amar404"]', '["Tuni5"]', 'team2', '{"Tuni5": "+121", "3amar404": "-56"}', 'approved', '2026-04-18T14:02:26.265326'),
  (20, '["3amar404", "momo", "old_kala5la5"]', '["SPoUD", "Tuni5", "zled"]', 'team1', '{"3amar404": "+57", "momo": "+64", "old_kala5la5": "+82", "SPoUD": "-47", "Tuni5": "-47", "zled": "-48"}', 'approved', '2026-04-18T14:42:44.383382'),
  (21, '["3amar404", "zled"]', '["momo", "SPoUD"]', 'team1', '{"3amar404": "+64", "zled": "+78", "momo": "-43", "SPoUD": "-46"}', 'approved', '2026-04-18T15:49:04.273683'),
  (22, '["3amar404", "De3ech", "momo", "old_kala5la5"]', '["barabi7", "SPoUD", "Tuni5", "zled"]', 'team2', '{"barabi7": "+56", "SPoUD": "+54", "Tuni5": "+56", "zled": "+55", "3amar404": "-33", "De3ech": "-41", "momo": "-36", "old_kala5la5": "-45"}', 'approved', '2026-04-19T21:29:10.439390'),
  (23, '["3amar404", "momo"]', '["De3ech", "SPoUD"]', 'team2', '{"De3ech": "+79", "SPoUD": "+72", "3amar404": "-37", "momo": "-41"}', 'approved', '2026-04-20T19:17:33.438862'),
  (24, '["3amar404", "zled"]', '["momo", "Tuni5"]', 'team2', '{"momo": "+71", "Tuni5": "+83", "3amar404": "-42", "zled": "-49"}', 'approved', '2026-04-21T21:19:02.691618'),
  (25, '["3amar404"]', '["zled"]', 'team2', '{"zled": "+131", "3amar404": "-73"}', 'approved', '2026-04-21T22:27:12.412328'),
  (26, '["3amar404"]', '["zled"]', 'team2', '{"zled": "+110", "3amar404": "-56"}', 'approved', '2026-04-21T23:19:03.776003'),
  (27, '["3amar404"]', '["Tuni5"]', 'team1', '{"3amar404": "+106", "Tuni5": "-109"}', 'approved', '2026-04-23T21:14:14.187326'),
  (28, '["3amar404", "barabi7", "momo"]', '["De3ech", "Tuni5", "zled"]', 'team1', '{"3amar404": "+50", "barabi7": "+73", "momo": "+62", "De3ech": "-51", "Tuni5": "-47", "zled": "-44"}', 'approved', '2026-04-23T21:45:23.288915'),
  (29, '["barabi7", "De3ech"]', '["momo", "zled"]', 'team1', '{"barabi7": "+30", "De3ech": "+30", "momo": "-12", "zled": "-12"}', 'approved', '2026-04-23T22:51:36.751210'),
  (30, '["3amar404", "De3ech", "Tuni5"]', '["barabi7", "momo", "zled"]', 'team1', '{"3amar404": "+45", "De3ech": "+67", "Tuni5": "+62", "barabi7": "-44", "momo": "-39", "zled": "-38"}', 'approved', '2026-04-30T19:43:00'),
  (31, '["3amar404", "barabi7", "Farouk", "momo"]', '["De3ech", "Tuni5", "zled"]', 'team2', '{"De3ech": "+84", "Tuni5": "+79", "zled": "+71", "3amar404": "-52", "barabi7": "-72", "Farouk": "-109", "momo": "-63"}', 'approved', '2026-04-30T20:34:00'),
  (32, '["3amar404", "Farouk", "zled"]', '["barabi7", "momo"]', 'team2', '{"barabi7": "+90", "momo": "+78", "3amar404": "-54", "Farouk": "-108", "zled": "-63"}', 'approved', '2026-04-30T21:24:00'),
  (33, '["3amar404", "barabi7"]', '["Farouk", "momo", "zled"]', 'team1', '{"3amar404": "+42", "barabi7": "+60", "Farouk": "-52", "momo": "-34", "zled": "-34"}', 'approved', '2026-05-02T00:02:00'),
  (34, '["3amar404", "momo", "zled"]', '["barabi7", "Farouk", "Tuni5"]', 'team2', '{"barabi7": "+22", "Farouk": "+34", "Tuni5": "+21", "3amar404": "-9", "momo": "-10", "zled": "-10"}', 'approved', '2026-05-02T22:35:00'),
  (35, '["3amar404", "barabi7"]', '["Farouk", "momo", "Tuni5"]', 'team1', '{"3amar404": "+48", "barabi7": "+67", "Farouk": "-63", "momo": "-40", "Tuni5": "-45"}', 'approved', '2026-05-02T22:39:00'),
  (36, '["3amar404", "momo"]', '["barabi7", "Farouk"]', 'team1', '{"3amar404": "+72", "momo": "+84", "barabi7": "-81", "Farouk": "-112"}', 'approved', '2026-05-02T23:20:00'),
  (37, '["3amar404", "zled"]', '["barabi7", "momo"]', 'team1', '{"3amar404": "+69", "zled": "+85", "barabi7": "-71", "momo": "-62"}', 'approved', '2026-05-03 23:50:00'),
  (38, '["3amar404", "Farouk", "zled"]', '["barabi7", "momo"]', 'team2', '{"barabi7": "+75", "momo": "+65", "3amar404": "-44", "Farouk": "-78", "zled": "-52"}', 'approved', '2026-05-04 00:52:00'),
  (39, '["3amar404", "barabi7", "momo"]', '["De3ech", "Farouk", "Tuni5", "zled"]', 'team2', '{"De3ech": "+49", "Farouk": "+59", "Tuni5": "+43", "zled": "+36", "3amar404": "-20", "barabi7": "-26", "momo": "-23"}', 'approved', '2026-05-04 19:44:44'),
  (40, '["3amar404", "De3ech", "Farouk", "zled"]', '["Tuni5", "barabi7", "momo"]', 'team2', '{"Tuni5": "+59", "barabi7": "+56", "momo": "+49", "3amar404": "-32", "De3ech": "-48", "Farouk": "-55", "zled": "-37"}', 'approved', '2026-05-04 20:43:41'),
  (41, '["3amar404", "momo", "old_kala5la5", "zled"]', '["De3ech", "Tuni5", "barabi7"]', 'team1', '{"3amar404": "+48", "momo": "+56", "old_kala5la5": "+103", "zled": "+57", "De3ech": "-60", "Tuni5": "-55", "barabi7": "-52"}', 'approved', '2026-05-04 21:32:05'),
  (42, '["3amar404", "zled"]', '["Tuni5", "momo"]', 'team2', '{"Tuni5": "+68", "momo": "+56", "3amar404": "-30", "zled": "-35"}', 'approved', '2026-05-06 21:59:19'),
  (43, '["3amar404", "Dex55", "Tuni5", "momo"]', '["barabi7", "old_kala5la5", "zled"]', 'team1', '{"3amar404": "+21", "Dex55": "+63", "Tuni5": "+31", "momo": "+25", "barabi7": "-19", "old_kala5la5": "-28", "zled": "-17"}', 'approved', '2026-05-09 22:47:55'),
  (44, '["3amar404", "barabi7", "old_kala5la5"]', '["Dex55", "Tuni5", "momo", "zled"]', 'team1', '{"3amar404": "+40", "barabi7": "+53", "old_kala5la5": "+84", "Dex55": "-72", "Tuni5": "-42", "momo": "-36", "zled": "-36"}', 'approved', '2026-05-10 00:14:54'),
  (45, '["3amar404", "momo", "zled"]', '["Dex55", "Tuni5", "barabi7"]', 'team1', '{"3amar404": "+58", "momo": "+68", "zled": "+69", "Dex55": "-132", "Tuni5": "-73", "barabi7": "-72"}', 'approved', '2026-05-13 22:18:48'),
  (46, '["3amar404", "momo", "zled"]', '["Dex55", "Tuni5", "barabi7"]', 'team2', '{"Dex55": "+53", "Tuni5": "+27", "barabi7": "+27", "3amar404": "-12", "momo": "-13", "zled": "-14"}', 'approved', '2026-05-13 23:54:12'),
  (47, '["De3ech", "Tuni5"]', '["barabi7", "momo"]', 'team2', '{"barabi7": "+65", "momo": "+56", "De3ech": "-53", "Tuni5": "-45"}', 'approved', '2026-05-14 11:58:32'),
  (48, '["3amar404", "De3ech", "Tuni5"]', '["SPoUD", "barabi7", "momo"]', 'team1', '{"3amar404": "+38", "De3ech": "+62", "Tuni5": "+50", "SPoUD": "-46", "barabi7": "-34", "momo": "-31"}', 'approved', '2026-05-14 13:18:00'),
  (49, '["3amar404", "SPoUD", "barabi7"]', '["De3ech", "Tuni5", "momo"]', 'team2', '{"De3ech": "+74", "Tuni5": "+61", "momo": "+52", "3amar404": "-36", "SPoUD": "-61", "barabi7": "-46"}', 'approved', '2026-05-14 14:40:26');

SELECT setval('players_id_seq', (SELECT MAX(id) FROM players));
SELECT setval('matches_id_seq', (SELECT MAX(id) FROM matches));
COMMIT;
