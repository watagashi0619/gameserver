import json
import uuid
from enum import Enum, IntEnum
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound

from .db import engine

MAX_USER_COUNT: int = 4


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


class NumberOfRoomMembers(BaseModel):
    members: int

    class Config:
        orm_mode = True


class RoomStatus(BaseModel):
    status: WaitRoomStatus

    class Config:
        orm_mode = True


def create_room(live_id: int, host_id: int) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO `room` (live_id, host_id, status) VALUES (:live_id, :host_id, :status)"
            ),
            {
                "live_id": live_id,
                "host_id": host_id,
                "status": int(WaitRoomStatus.Waiting),
            },
        )
    return result.lastrowid


def list_room(live_id: int) -> list[RoomInfo]:
    with engine.begin() as conn:
        if live_id == 0:
            query = "SELECT `room_id`,count(`room_id`),`live_id` FROM room,room_member WHERE `id`=`room_id` GROUP BY `room_id`"
            result = conn.execute(text(query))
        else:
            query = "SELECT `room_id`,count(`room_id`),`live_id` FROM room,room_member WHERE `id`=`room_id` AND `room_id`=ANY(SELECT `id` FROM `room` WHERE `live_id`=:live_id) GROUP BY `room_id`"
            result = conn.execute(
                text(query),
                {"live_id": live_id},
            )
        return [
            RoomInfo(
                room_id=item[0],
                live_id=item[1],
                joined_user_count=item[2],
                max_user_count=MAX_USER_COUNT,
            )
            for item in result.fetchall()
        ]


def join_room(
    room_id: int, user_id: int, select_difficulty: LiveDifficulty
) -> JoinRoomResult:
    with engine.begin() as conn:
        try:
            query = "SELECT * FROM `room` WHERE `id`=:room_id"
            result = conn.execute(
                text(query),
                {"room_id": room_id},
            )
            print(result.one())
            if result is None:
                return JoinRoomResult.Disbanded
            status = _get_room_status(conn, room_id).status
            if status != WaitRoomStatus.Waiting:
                return JoinRoomResult.OtherError
            # members = _get_number_of_room_members(conn, room_id).members
            members = _get_number_of_room_members(conn, room_id)
            if members >= 4:
                return JoinRoomResult.RoomFull
            _join_room(conn, room_id, user_id, select_difficulty)
            return JoinRoomResult.Ok
        except Exception as e:
            return JoinRoomResult.OtherError


def _join_room(conn, room_id: int, user_id: int, select_difficulty: LiveDifficulty):
    query = "INSERT INTO `room_member` (room_id, user_id, select_difficulty) VALUES (:room_id, :user_id, :select_difficulty)"
    conn.execute(
        text(query),
        {
            "room_id": room_id,
            "user_id": user_id,
            "select_difficulty": int(select_difficulty),
        },
    )


def _get_room_status(conn, room_id: int):
    query = "SELECT `status` FROM `room` WHERE `id`=:room_id"
    result = conn.execute(text(query), {"room_id": room_id})
    try:
        row = result.one()
    except NoResultFound:
        return None
    return RoomStatus.from_orm(row)


def _get_number_of_room_members(conn, room_id: int):
    query = "SELECT COUNT(`room_id`) FROM `room_member` WHERE `room_id`=:room_id"
    result = conn.execute(text(query), {"room_id": room_id})
    # try:
    #    row = result.one()
    # except NoResultFound:
    #    return None
    # return NumberOfRoomMembers.from_orm(row)
    return result.one()[0]
