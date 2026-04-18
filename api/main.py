from fastapi import FastAPI

app = FastAPI()


@app.get("/")
async def root():
    return {"message": "who calling this func?"}


@app.get("/simon")
async def root():
    return ({"message": 3 + 3})
    #return {"message": f"who calling this func, SIMON?"}