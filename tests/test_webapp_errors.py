from errors import BizError, CODE_EMPTY_QUESTION, CODE_OK, error_body


def test_error_body_has_uniform_shape():
    body = error_body(CODE_OK, "ok", "trace-1")
    assert body == {
        "code": 0,
        "message": "ok",
        "data": None,
        "trace_id": "trace-1",
    }


def test_biz_error_carries_code_message_and_http_status():
    error = BizError(CODE_EMPTY_QUESTION, "问题不能为空", 400)
    assert error.code == CODE_EMPTY_QUESTION
    assert error.message == "问题不能为空"
    assert error.http_status == 400
    assert str(error) == "问题不能为空"
