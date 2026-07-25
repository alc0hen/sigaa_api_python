from enum import Enum, auto
class InstitutionType(Enum):
    IFSC = "IFSC"
    IFAL = "IFAL"
    UFAL = "UFAL"
    UFPE = "UFPE"
    UFPB = "UFPB"
    UNB = "UNB"
class HTTPMethod(Enum):
    GET = "GET"
    POST = "POST"
