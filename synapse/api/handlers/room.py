# -*- coding: utf-8 -*-
"""Contains functions for performing events on rooms."""
from twisted.internet import defer

from synapse.api.errors import RoomError
from synapse.api.event_store import StoreException
from synapse.api.events.room import RoomTopicEvent, MessageEvent
from . import BaseHandler

import json
import time


class Membership(object):

    """An enum representing the membership state of a user in a room."""
    INVITE = "invite"
    JOIN = "join"
    KNOCK = "knock"
    LEAVE = "leave"


class MessageHandler(BaseHandler):

    @defer.inlineCallbacks
    def get_message(self, event=None):
        """ Retrieve a message.

        Args:
            event : A message event.
        Returns:
            The message, or None if no message exists.
        Raises:
            RoomError if something went wrong.
        """
        # self.auth.check(event)

        if event.auth_user_id:
            # check they are joined in the room
            yield _get_joined_or_throw(self.store,
                                       room_id=event.room_id,
                                       user_id=event.auth_user_id)

        # Pull out the message from the db
        results = yield self.store.get_message(room_id=event.room_id,
                                               msg_id=event.msg_id,
                                               user_id=event.user_id)

        if results:
            defer.returnValue(results[0])
        defer.returnValue(None)

    @defer.inlineCallbacks
    def send_message(self, event=None):
        """ Send a message.

        Args:
            event : The message event to store.
        Raises:
            SynapseError if something went wrong.
        """
        if event.auth_user_id:
            # verify they are sending msgs under their own user id
            if event.user_id != event.auth_user_id:
                raise RoomError(403, "Must send messages as yourself.")

            # Check if sender_id is in room room_id
            yield _get_joined_or_throw(self.store,
                                       room_id=event.room_id,
                                       user_id=event.auth_user_id)

        # store message in db
        yield self.store.store_message(user_id=event.user_id,
                                       room_id=event.room_id,
                                       msg_id=event.msg_id,
                                       content=json.dumps(event.content))

    @defer.inlineCallbacks
    def store_room_path_data(self, event=None, path=None):
        """ Stores data for a room under a given path.

        Args:
            event : The room path event
            path : The path which can be used to retrieve the data.
        Raises:
            SynapseError if something went wrong.
        """
        if event.auth_user_id:
            # check they are joined in the room
            yield _get_joined_or_throw(self.store,
                                       room_id=event.room_id,
                                       user_id=event.auth_user_id)

        # store in db
        yield self.store.store_path_data(room_id=event.room_id,
                                         path=path,
                                         content=json.dumps(event.content))

    @defer.inlineCallbacks
    def get_room_path_data(self, event=None, path=None,
                           public_room_rules=[],
                           private_room_rules=["join"]):
        """ Get path data from a room.

        Args:
            event : The room path event
            path : The path the data was stored under.
            public_room_rules : A list of membership states the user can be in,
            in order to read this data IN A PUBLIC ROOM. An empty list means
            'any state'.
            private_room_rules : A list of membership states the user can be in,
            in order to read this data IN A PRIVATE ROOM. An empty list means
            'any state'.
        Returns:
            The path data content.
        Raises:
            SynapseError if something went wrong.
        """
        if event.type == RoomTopicEvent.TYPE:
            # anyone invited/joined can read the topic
            private_room_rules = ["invite", "join"]

        # does this room exist
        room = yield self.store.get_room(event.room_id)
        if not room:
            raise RoomError(403, "Room does not exist.")
        room = room[0]

        # does this user exist in this room
        member = yield self.store.get_room_member(
            room_id=event.room_id,
            user_id="" if not event.auth_user_id else event.auth_user_id)

        member_state = member[0].membership if member else None

        if room.is_public and public_room_rules:
            # make sure the user meets public room rules
            if member_state not in public_room_rules:
                raise RoomError(403, "Member does not meet public room rules.")
        elif not room.is_public and private_room_rules:
            # make sure the user meets private room rules
            if member_state not in private_room_rules:
                raise RoomError(
                    403, "Member does not meet private room rules.")

        data = yield self.store.get_path_data(path)
        defer.returnValue(data)


class RoomCreationHandler(BaseHandler):

    @defer.inlineCallbacks
    def create_room(self, user_id=None, room_id=None, config=None):
        """ Creates a new room.

        Args:
            user_id (str): The ID of the user creating the new room.
            room_id (str): The proposed ID for the new room. Can be None, in
            which case one will be created for you.
            config (dict) : A dict of configuration options.
        Returns:
            The new room ID.
        Raises:
            RoomError if the room ID was taken, couldn't be stored, or something
            went horribly wrong.
        """
        try:
            new_room_id = yield self.store.store_room(
                room_id=room_id,
                room_creator_user_id=user_id,
                is_public=config["visibility"] == "public"
            )
            if not new_room_id:
                raise RoomError(409, "Room ID in use.")

            defer.returnValue(new_room_id)
        except StoreException:
            raise RoomError(500, "Unable to create room.")


class RoomMemberHandler(BaseHandler):

    @defer.inlineCallbacks
    def get_room_member(self, event=None):
        """Retrieve a room member from a room.

        Args:
            event : The room member event
        Returns:
            The room member, or None if this member does not exist.
        Raises:
            RoomError if something goes wrong.
        """
        if event.auth_user_id:
            # check they are joined in the room
            yield _get_joined_or_throw(self.store,
                                       room_id=event.room_id,
                                       user_id=event.auth_user_id)

        member = yield self.store.get_room_member(user_id=event.user_id,
                                                  room_id=event.room_id)
        if member:
            defer.returnValue(member[0])
        defer.returnValue(member)

    @defer.inlineCallbacks
    def change_membership(self, event=None, broadcast_msg=False):
        """ Change the membership status of a user in a room.

        Args:
            event (SynapseEvent): The membership event
            broadcast_msg (bool): True to inject a membership message into this
            room on success.
        Raises:
            RoomError if there was a problem changing the membership.
        """
        # does this room even exist
        room = self.store.get_room(event.room_id)
        if not room:
            raise RoomError(403, "Room does not exist")

        # get info about the caller
        try:
            caller = yield self.store.get_room_member(
                user_id=event.auth_user_id,
                room_id=event.room_id)
        except:
            pass
        caller_in_room = caller and caller[0].membership == "join"

        # get info about the target
        try:
            target = yield self.store.get_room_member(
                user_id=event.user_id,
                room_id=event.room_id)
        except:
            pass
        target_in_room = target and target[0].membership == "join"

        if Membership.INVITE == event.membership:
            # Invites are valid iff caller is in the room and target isn't.
            if not caller_in_room or target_in_room:
                # caller isn't joined or the target is already in the room.
                raise RoomError(403, "Cannot invite.")
        elif Membership.JOIN == event.membership:
            # Joins are valid iff caller == target and they were:
            # invited: They are accepting the invitation
            # joined: It's a NOOP
            if (event.auth_user_id != event.user_id or not caller or
                    caller[0].membership not in
                    [Membership.INVITE, Membership.JOIN]):
                raise RoomError(403, "Cannot join.")
        elif Membership.LEAVE == event.membership:
            if not caller_in_room or event.user_id != event.auth_user_id:
                # trying to leave a room you aren't joined or trying to force
                # another user to leave
                raise RoomError(403, "Cannot leave.")
        else:
            raise RoomError(500, "Unknown membership %s" % event.membership)

        # store membership
        yield self.store.store_room_member(
            user_id=event.user_id,
            room_id=event.room_id,
            content=event.content,
            membership=event.membership)

        if broadcast_msg:
            yield self._inject_membership_msg(
                source=event.auth_user_id,
                target=event.user_id,
                room_id=event.room_id,
                membership=event.membership)

    @defer.inlineCallbacks
    def _inject_membership_msg(self, room_id=None, source=None, target=None,
                               membership=None):
        # TODO this should be a different type of message, not sy.text
        if membership == Membership.INVITE:
            body = "%s invited %s to the room." % (source, target)
        elif membership == Membership.JOIN:
            body = "%s joined the room." % (target)
        elif membership == Membership.LEAVE:
            body = "%s left the room." % (target)
        else:
            raise RoomError(500, "Unknown membership value %s" % membership)

        membership_json = {
            "msgtype": u"sy.text",
            "body": body
        }
        msg_id = "m%s" % int(time.time())

        event = self.event_factory.create_event(
                etype=MessageEvent.TYPE,
                room_id=room_id,
                user_id="_hs_",
                msg_id=msg_id,
                auth_user_id=None,
                content=membership_json
                )

        handler = MessageHandler(self.store, self.event_factory)
        yield handler.send_message(event)


@defer.inlineCallbacks
def _get_joined_or_throw(store=None, user_id=None, room_id=None):
    """Utility method to return the specified room member.

    Args:
        store : The event data store
        user_id : The member's ID
        room_id : The room where the member is joined.
    Returns:
        The room member.
    Raises:
        RoomError if this member does not exist/isn't joined.
    """
    member = yield store.get_room_member(
        room_id=room_id,
        user_id=user_id)
    if not member or member[0].membership != "join":
        raise RoomError(403, "Haven't joined room.'")
    defer.returnValue(member)
