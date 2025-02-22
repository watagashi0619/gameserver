import json
import threading
import time
import uuid
from enum import Enum, IntEnum
from sys import int_info
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel, NoneIsAllowedError
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound

from .db import engine

MAX_USER_COUNT: int = 4
TIMEOUT_FROM_START: int = 150  # 曲の最長は 135
TIMEOUT_FROM_END: int = 10


class InvalidToken(Exception):
    """指定されたtokenが不正だったときに投げる"""


class SafeUser(BaseModel):
    """token を含まないUser"""

    id: int
    name: str
    leader_card_id: int

    class Config:
        orm_mode = True


def create_user(name: str, leader_card_id: int) -> str:
    """Create new user and returns their token"""
    token = str(uuid.uuid4())
    # NOTE: tokenが衝突したらリトライする必要がある.
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO `user` (name, token, leader_card_id) VALUES (:name, :token, :leader_card_id)"
            ),
            {"name": name, "token": token, "leader_card_id": leader_card_id},
        )
        # print(result)
    return token


def _get_user_by_token(conn, token: str) -> Optional[SafeUser]:
    # TODO: 実装
    result = conn.execute(
        text("SELECT `id`,`name`,`leader_card_id` FROM `user` WHERE `token`=:token"),
        dict(token=token),
    )
    try:
        row = result.one()
    except NoResultFound:
        return None
    return SafeUser.from_orm(row)


def get_user_by_token(token: str) -> Optional[SafeUser]:
    with engine.begin() as conn:
        return _get_user_by_token(conn, token)


def update_user(token: str, name: str, leader_card_id: int) -> None:
    # このコードを実装してもらう
    with engine.begin() as conn:
        # TODO: 実装
        result = conn.execute(
            text(
                "UPDATE `user` SET `name`=:name, `leader_card_id`=:leader_card_id WHERE `token`=:token"
            ),
            {"token": token, "name": name, "leader_card_id": leader_card_id},
        )


# Room


class LiveDifficulty(IntEnum):
    normal = 1
    hard = 2


class JoinRoomResult(IntEnum):
    Ok = 1  # 入場OK
    RoomFull = 2  # 満員
    Disbanded = 3  # 解散済み
    OtherError = 4  # その他エラー


class WaitRoomStatus(IntEnum):
    Waiting = 1  # ホストがライブ開始ボタン押すのを待っている
    LiveStart = 2  # ライブ画面遷移OK
    Dissolution = 3  # 解散された


class RoomInfo(BaseModel):
    room_id: int  # 部屋識別子
    live_id: int  # プレイ対象の楽曲識別子
    joined_user_count: int  # 部屋に入っている人数
    max_user_count: int  # 部屋の最大人数


class RoomUser(BaseModel):
    user_id: int  # ユーザー識別子
    name: str  # ユーザー名
    leader_card_id: int  # 設定アバター
    select_difficulty: LiveDifficulty  # 選択難易度
    is_me: bool  # リクエスト投げたユーザーと同じか
    is_host: bool  # 部屋を立てた人か


class ResultUser(BaseModel):
    user_id: int  # ユーザー識別子
    judge_count_list: list[int]  # 各判定数（良い判定から昇順）
    score: int  # 獲得スコア


def create_room(live_id: int, host_id: int) -> int:
    with engine.begin() as conn:
        query = "INSERT INTO `room` (live_id, host_id, status) VALUES (:live_id, :host_id, :status)"
        result = conn.execute(
            text(query),
            {
                "live_id": live_id,
                "host_id": host_id,
                "status": int(WaitRoomStatus.Waiting),
            },
        )
    return result.lastrowid


def list_room(live_id: int) -> list[RoomInfo]:
    with engine.begin() as conn:
        args = {"status": int(WaitRoomStatus.Waiting)}
        query = "SELECT `room_id`,`live_id`,count(`room_id`) AS `joined_user_count` FROM room,room_member WHERE `id`=`room_id` AND `status`=:status "
        if live_id != 0:
            query += "AND `live_id`=:live_id "
            args["live_id"] = live_id
        query += "GROUP BY `room_id`"
        result = conn.execute(text(query), args)

        return [
            RoomInfo(
                room_id=row.room_id,
                live_id=row.live_id,
                joined_user_count=row.joined_user_count,
                max_user_count=MAX_USER_COUNT,
            )
            for row in result.fetchall()
        ]


def join_room(
    room_id: int, user_id: int, select_difficulty: LiveDifficulty
) -> JoinRoomResult:
    with engine.begin() as conn:
        try:
            query = "SELECT * FROM `room` WHERE `id`=:room_id FOR UPDATE"
            result = conn.execute(text(query), {"room_id": room_id})
            if result is None:
                return JoinRoomResult.Disbanded
            status = _get_room_status(conn, room_id)
            if status != WaitRoomStatus.Waiting:
                return JoinRoomResult.OtherError
            members = _get_number_of_room_members(conn, room_id)
            if members >= MAX_USER_COUNT:
                return JoinRoomResult.RoomFull
            _insert_into_room_member(conn, room_id, user_id, select_difficulty)
            return JoinRoomResult.Ok
        except Exception as e:
            return JoinRoomResult.OtherError


def get_room_status(room_id: int) -> Optional[WaitRoomStatus]:
    with engine.begin() as conn:
        return _get_room_status(conn, room_id)


def get_room_users(room_id: int, req_user_id: int) -> list[RoomUser]:
    with engine.begin() as conn:
        return _get_room_users(conn, room_id, req_user_id)


def start_room(room_id: int) -> None:
    with engine.begin() as conn:
        _update_room_status(conn, room_id)
    thread = threading.Thread(
        target=_timeout_threading, args=(conn, room_id, TIMEOUT_FROM_START)
    )
    thread.start()


def end_room(
    room_id: int, user_id: int, judge_count_list: list[int], score: int
) -> None:
    with engine.begin() as conn:
        _update_room_member_scores(conn, room_id, user_id, judge_count_list, score)
        if _get_room_status(conn, room_id) == WaitRoomStatus.LiveStart:
            thread = threading.Thread(
                target=_timeout_threading, args=(conn, room_id, TIMEOUT_FROM_END)
            )
            thread.start()


def result_room(room_id: int) -> list[ResultUser]:
    with engine.begin() as conn:
        return _get_results_from_room_id(conn, room_id)


def leave_room(room_id: int, user_id: int) -> None:
    with engine.begin() as conn:
        _delete_room_member(conn, room_id, user_id)
        if _get_number_of_room_members(conn, room_id) == 0:
            _delete_room(conn, room_id)
        elif _get_host_id(conn, room_id) == user_id:
            _change_host(conn, room_id)


def _insert_into_room_member(
    conn, room_id: int, user_id: int, select_difficulty: LiveDifficulty
) -> None:
    query = "INSERT INTO `room_member` (room_id, user_id, select_difficulty) VALUES (:room_id, :user_id, :select_difficulty)"
    conn.execute(
        text(query),
        {
            "room_id": room_id,
            "user_id": user_id,
            "select_difficulty": int(select_difficulty),
        },
    )


def _get_room_status(conn, room_id: int) -> Optional[WaitRoomStatus]:
    query = "SELECT `status` FROM `room` WHERE `id`=:room_id"
    result = conn.execute(text(query), {"room_id": room_id})
    try:
        return result.scalar()
    except NoResultFound:
        return None


def _get_number_of_room_members(conn, room_id: int) -> int:
    query = (
        "SELECT COUNT(`room_id`) FROM `room_member` WHERE `room_id`=:room_id FOR UPDATE"
    )
    result = conn.execute(text(query), {"room_id": room_id})
    return result.scalar()


def _get_room_users(conn, room_id: int, req_user_id: int = None) -> list[RoomUser]:
    query = "SELECT `user_id`,`name`,  `leader_card_id`,`select_difficulty` FROM `user`,`room_member` WHERE `id`=`user_id` AND `room_id`=:room_id"
    result = conn.execute(text(query), {"room_id": room_id})

    host_id = _get_host_id(conn, room_id)

    return [
        RoomUser(
            user_id=row.user_id,
            name=row.name,
            leader_card_id=row.leader_card_id,
            select_difficulty=row.select_difficulty,
            is_me=(req_user_id == row.user_id),
            is_host=(host_id == row.user_id),
        )
        for row in result.fetchall()
    ]


def _update_room_status(conn, room_id: int) -> None:
    query = "UPDATE `room` SET `status`=:status WHERE `id`=:room_id"
    conn.execute(
        text(query), {"status": int(WaitRoomStatus.LiveStart), "room_id": room_id}
    )


def _update_room_member_scores(
    conn, room_id: int, user_id: int, judge_count_list: list[int], score: int
) -> None:
    query = "UPDATE `room_member` SET `judge_perfect`=:judge_perfect, `judge_great`=:judge_great, `judge_good`=:judge_good, `judge_bad`=:judge_bad, `judge_miss`=:judge_miss, `score`=:score WHERE `room_id`=:room_id AND `user_id`=:user_id"
    conn.execute(
        text(query),
        {
            "room_id": room_id,
            "user_id": user_id,
            "judge_perfect": judge_count_list[0],
            "judge_great": judge_count_list[1],
            "judge_good": judge_count_list[2],
            "judge_bad": judge_count_list[3],
            "judge_miss": judge_count_list[4],
            "score": score,
        },
    )


def _timeout_threading(conn, room_id: int, timeout: int):
    time.sleep(timeout)
    with engine.begin() as conn:
        _update_null_to_zero(conn, room_id)


def _update_null_to_zero(conn, room_id: int):
    query = "UPDATE `room_member` SET `judge_perfect`=:judge_perfect, `judge_great`=:judge_great, `judge_good`=:judge_good, `judge_bad`=:judge_bad, `judge_miss`=:judge_miss, `score`=:score WHERE `room_id`=:room_id AND `score` IS NULL"
    conn.execute(
        text(query),
        {
            "room_id": room_id,
            "judge_perfect": 0,
            "judge_great": 0,
            "judge_good": 0,
            "judge_bad": 0,
            "judge_miss": 0,
            "score": 0,
        },
    )


def _get_results_from_room_id(conn, room_id: int) -> list[ResultUser]:
    result_user_list = []
    for room_user in _get_room_users(conn, room_id):
        query = "SELECT `user_id`, `judge_perfect`, `judge_great`, `judge_good`, `judge_bad`, `judge_miss`, `score` FROM `room_member` WHERE `room_id`=:room_id AND `user_id`=:user_id"
        result = conn.execute(
            text(query), {"room_id": room_id, "user_id": room_user.user_id}
        )

        row = result.one()

        if row.score is None:
            return []

        result_user_list.append(
            ResultUser(user_id=row.user_id, judge_count_list=row[1:-1], score=row.score)
        )
    query = "UPDATE `room` SET `status`=:status WHERE `id`=:room_id"
    conn.execute(
        text(query), {"room_id": room_id, "status": int(WaitRoomStatus.Dissolution)}
    )

    return result_user_list


def _delete_room_member(conn, room_id: int, user_id: int) -> None:
    query = "DELETE FROM `room_member` WHERE `room_id`=:room_id AND `user_id`=:user_id"
    conn.execute(text(query), {"room_id": room_id, "user_id": user_id})


def _delete_room(conn, room_id: int) -> None:
    query = "DELETE FROM `room` WHERE `id`=:room_id"
    conn.execute(text(query), {"room_id": room_id})


def _get_host_id(conn, room_id: int) -> int:
    query = "SELECT `host_id` FROM `room` WHERE `id`=:room_id"
    result = conn.execute(text(query), {"room_id": room_id})
    return result.scalar()


def _change_host(conn, room_id: int) -> None:
    query = "SELECT `user_id` FROM `room_member` WHERE `room_id`=:room_id"
    result = conn.execute(text(query), {"room_id": room_id})
    row = result.first()
    query = "UPDATE `room` SET `host_id`=:new_host WHERE `id`=:room_id"
    conn.execute(text(query), {"new_host": row.user_id, "room_id": room_id})
