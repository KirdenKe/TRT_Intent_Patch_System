from tools.m12_verify_n8n_access import has_human_facing_chat_response


def test_async_response_node_handshake_is_not_a_chat_response():
    assert not has_human_facing_chat_response(
        {
            "executionId": "1815",
            "executionStarted": True,
            "resumeToken": "token",
        }
    )


def test_operator_message_is_a_chat_response():
    assert has_human_facing_chat_response({"operator_message": "How can I help?"})

