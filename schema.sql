DROP TABLE IF EXISTS `user`;
CREATE TABLE `user` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `token` varchar(255) NOT NULL,
  `leader_card_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `token` (`token`)
);

DROP TABLE IF EXISTS `room`;
CREATE TABLE `room` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `live_id` int NOT NULL,
  `host_id` bigint NOT NULL,
  `status` int NOT NULL,
  PRIMARY KEY (`id`)
);

DROP TABLE IF EXISTS `room_member`;
CREATE TABLE `room_member` (
  `room_id` bigint NOT NULL,
  `user_id` int NOT NULL,
  `select_difficulty` int NOT NULL,
  `judge_perfect` int DEFAULT NULL,
  `judge_great` int DEFAULT NULL,
  `judge_good` int DEFAULT NULL,
  `judge_bad` int DEFAULT NULL,
  `judge_miss` int DEFAULT NULL,
  `score` int DEFAULT NULL,
  PRIMARY KEY (`room_id`, `user_id`)
);